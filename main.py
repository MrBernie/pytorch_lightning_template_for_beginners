import os

os.environ['TORCH_CUDNN_V8_API_ENABLED'] = '1'  # 允许使用BF16精度进行训练。enable bf16 precision in pytorch 1.12, see https://github.com/Lightning-AI/lightning/issues/11933#issuecomment-1181590004
os.environ["OMP_NUM_THREADS"] = str(1)  # 限制进程数量，放在import torch和numpy之前。不加会导致程序占用特别多的CPU资源，使得服务器变卡。
# limit the threads to reduce cpu overloads, will speed up when there are lots of CPU cores on the running machine

from typing import *

import torch
from torch import Tensor
# torch 1.12开始，TF32默认关闭，下面的参数会打开TF32。对于A100，使用TF32会使得速度得到很大的提升，同时不影响训练结果【或轻微影响】。
torch.backends.cuda.matmul.allow_tf32 = True  # The flag below controls whether to allow TF32 on matmul. This flag defaults to False in PyTorch 1.12 and later.
torch.backends.cudnn.allow_tf32 = True  # The flag below controls whether to allow TF32 on cuDNN. This flag defaults to True.
torch.set_float32_matmul_precision('high')

from jsonargparse import lazy_instance
from pytorch_lightning import LightningDataModule, LightningModule
from pytorch_lightning.cli import LightningArgumentParser, LightningCLI
from pytorch_lightning.utilities.rank_zero import rank_zero_info
from torch.utils.data import DataLoader, Dataset
from packaging.version import Version

from utils import MyRichProgressBar as RichProgressBar
from utils import MyLogger as TensorBoardLogger
from utils import tag_and_log_git_status
from utils.my_save_config_callback import MySaveConfigCallback as SaveConfigCallback
from utils.flops import write_FLOPs


class RandomDataset(Dataset):
    """一个随机数组成的数据集，此处用于展示其他模块的功能
    """

    def __init__(self, length, size: List[int]):
        self.len = length
        self.data = torch.randn(length, *size)

    def __getitem__(self, index):
        return self.data[index]

    def __len__(self):
        return self.len


class MyDataModule(LightningDataModule):
    """定义了如何生成训练、验证、测试、以及推理时的DataLoader。此处使用RandomDataset来生成数据。
    LightningDataModule相关资料：https://pytorch-lightning.readthedocs.io/en/stable/api/pytorch_lightning.core.LightningDataModule.html
    """

    def __init__(self, input_size: List[int] = [8, 16000 * 4], num_workers: int = 5, batch_size: Tuple[int, int] = (2, 4)):
        super().__init__()
        self.input_size = input_size  # 8通道4s采样率16000Hz的语音
        self.num_workers = num_workers
        self.batch_size = batch_size  # train: batch_size[0]; test: batch_size[1]

    def prepare_data(self) -> None:
        return super().prepare_data()

    def train_dataloader(self) -> DataLoader:
        return DataLoader(RandomDataset(640, self.input_size), batch_size=self.batch_size[0], num_workers=self.num_workers, shuffle=True)

    def val_dataloader(self) -> DataLoader:
        return DataLoader(RandomDataset(640, self.input_size), batch_size=self.batch_size[1], num_workers=self.num_workers)

    def test_dataloader(self) -> DataLoader:
        return DataLoader(RandomDataset(640, self.input_size), batch_size=1, num_workers=self.num_workers)


class MyArch(torch.nn.Module):
    """这个类定义了网络结构，此处是Conv1d。也可以将网络结构写到LightningModule里面。
    """

    def __init__(self, input_size: int = 8, output_size: int = 2) -> None:
        super().__init__()
        self.conv = torch.nn.Conv1d(in_channels=input_size, out_channels=output_size, kernel_size=5)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.conv.forward(x)


class MyModel(LightningModule):
    """LightningModule控制了模型训练的各个方面，即定义了如何使用一个mini-batch的数据进行训练、验证、测试、推理，以及训练使用的optimizer。
    
    LightningModule内部具有特定名字的函数（如下面涉及的on_train_start、training_step等）会被lightning框架在函数名称对应的阶段自动调用。
    
    LightningModule的相关资料：https://pytorch-lightning.readthedocs.io/en/stable/common/lightning_module.html
    """

    def __init__(self, arch: MyArch = lazy_instance(MyArch), exp_name: str = "exp", compile: bool = False):
        super().__init__()
        if compile != False:
            assert compile is True or compile == 'disable', compile
            assert Version(torch.__version__) >= Version('2.0.0'), torch.__version__
            if compile == 'disable':
                rank_zero_info('compile is disabled for testing with dynamic shape')
            # pytorch 2.0 新出的compile功能，编译完成之后的模型速度更快
            # 目前compile对于动态输入（如变长）的支持还不够好，后期功能稳定之后可以给dynamic=True
            # dynamic=False的情况下，如果输入的shape会不断变化，如语音长度不定，会不断触发编译，导致速度极度下降。
            # 因此要么训练集和验证集定长（测试集可以等训练完成之后，compile给disable），要么训练的时候不用compile
            self.arch = torch.compile(arch, disable=True if compile == 'disable' else False)
        else:
            self.arch = arch

        # save all the parameters to self.hparams
        self.save_hyperparameters(ignore=['arch'])

    def forward(self, x):
        return self.arch(x)

    def on_load_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        # 因为目前compile的模型，在测试时如果存在变长，需要设置compile=disable/False，这个时候模型参数的加载需要去除掉参数名里面的_orig_mod
        if self.compile == 'disable' or self.compile is False:
            # load weights for case compile==disable/False from compiled checkpoint
            state_dict = checkpoint['state_dict']
            state_dict_new = dict()
            for k, v, in state_dict.items():
                state_dict_new[k.replace('_orig_mod.', '')] = v  # rename weights to remove _orig_mod in name
            checkpoint['state_dict'] = state_dict_new
        return super().on_load_checkpoint(checkpoint)

    def on_train_start(self):
        if self.current_epoch == 0:
            if self.trainer.is_global_zero and hasattr(self.logger, 'log_dir') and 'notag' not in self.hparams.exp_name:
                # 在当前训练的程序代码树上添加Git标签，使得代码版本可以与训练version对应起来。【注意先commit内容修改，然后再训练。测试的时候exp_name设置为notag】
                # note: if change self.logger.log_dir to self.trainer.log_dir, the training will stuck on multi-gpu training
                tag_and_log_git_status(self.logger.log_dir + '/git.out', self.logger.version, self.hparams.exp_name, model_name=type(self).__name__)

            if self.trainer.is_global_zero and hasattr(self.logger, 'log_dir'):
                # 输出模型到 model.txt
                with open(self.logger.log_dir + '/model.txt', 'a') as f:
                    f.write(str(self))
                    f.write('\n\n\n')
                # measure the model FLOPs
                write_FLOPs(model=self, save_dir=self.logger.log_dir, num_chns=8, fs=16000, audio_time_len=4, model_import_path='main.MyModel')

    def training_step(self, batch: Tensor, batch_idx: int):
        """如何使用一个mini-batch的数据得到train/loss。其他step同理。

        Args:
            batch: train DataLoader给出一个mini-batch的数据
        """
        preds = self.forward(batch)

        if self.trainer.precision == '16-mixed' or self.trainer.precision == 'bf16-mixed':
            # 启用半精度训练的时候，某些loss函数可能需要开启32精度来计算
            with torch.autocast(device_type=self.device.type, dtype=torch.float32):
                loss = preds.sum()  # convert to float32 to avoid numerical problem in loss calculation
        else:
            loss = preds.sum()

        self.log("train/loss", loss, on_epoch=True, prog_bar=True)
        return loss

    def validation_step(self, batch: Tensor, batch_idx: int):
        if self.trainer.precision == '16-mixed' or self.trainer.precision == 'bf16-mixed':
            # 在validation_step和test_step里面，临时启用32精度
            autocast = torch.autocast(device_type=self.device.type, dtype=torch.float32)
            autocast.__enter__()

        preds = self.forward(batch)
        loss = preds.sum()
        self.log("val/loss", loss, on_epoch=True, prog_bar=True, sync_dist=True)  # 设置sync_dist=True，使得val/loss在多卡训练的时候能够同步，用于选择最佳的checkpoint等任务。train/loss不需要设置这个，因为训练步需要同步的是梯度，而不是指标，梯度会自动同步

        if self.trainer.precision == '16-mixed' or self.trainer.precision == 'bf16-mixed':
            autocast.__exit__(None, None, None)  # 关闭32精度

    def test_step(self, batch: Tensor, batch_idx: int):
        if self.trainer.precision == '16-mixed' or self.trainer.precision == 'bf16-mixed':
            # 在validation_step和test_step里面，临时启用32精度
            autocast = torch.autocast(device_type=self.device.type, dtype=torch.float32)
            autocast.__enter__()

        preds = self.forward(batch)
        loss = preds.sum()
        self.log("test/loss", loss, on_epoch=True, prog_bar=True)

        if self.trainer.precision == '16-mixed' or self.trainer.precision == 'bf16-mixed':
            autocast.__exit__(None, None, None)  # 关闭32精度

    def predict_step(self, batch: Tensor, batch_idx: int):
        preds = self.forward(batch)
        return preds

    def configure_optimizers(self):
        if self.trainer.precision == '16-mixed':
            # according to https://discuss.pytorch.org/t/adam-half-precision-nans/1765
            # 半精度（FP16）训练的时候，需要将优化器默认的eps从1e-8改为1e-4，因为1e-8在FP16表示下等于0
            # 如果优化器没有eps参数则跳过
            optimizer = torch.optim.Adam(self.arch.parameters(), lr=0.001, eps=1e-4)
            rank_zero_info('setting the eps of Adam to 1e-4 for FP16 mixed precision training')
        else:
            optimizer = torch.optim.Adam(self.arch.parameters(), lr=0.001)

        # 不需要学习率调整
        # return optimizer
        # 需要调整学习率
        lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer=optimizer, mode='min', patience=5, cooldown=5)
        return {
            'optimizer': optimizer,
            'lr_scheduler': {
                'scheduler': lr_scheduler,
                'monitor': 'val/loss',
            }
        }


class MyCLI(LightningCLI):
    """命令行接口（Command Line Interface）：命令行参数分析、Trainer的构建、Trainer命令（训练、测试、推理）的执行等等
    Trainer的相关资料: https://pytorch-lightning.readthedocs.io/en/stable/common/trainer.html
    CLI的相关资料：https://pytorch-lightning.readthedocs.io/en/stable/common/lightning_cli.html
    """

    def add_arguments_to_parser(self, parser: LightningArgumentParser) -> None:
        """添加、设置默认的参数，需要使用的callback（此处callback的意思是在LightningModule各个step函数开始执行、结束执行等时机想要被调用的具有特殊功能的函数，这些函数被封装到了不同的callbacks类里面，如EarlyStopping、ModelCheckpoint）。  
        callback的相关资料: https://pytorch-lightning.readthedocs.io/en/stable/extensions/callbacks.html
        """
        from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint, LearningRateMonitor

        parser.set_defaults({"trainer.strategy": "ddp_find_unused_parameters_false"})
        parser.set_defaults({"trainer.accelerator": "gpu"})
        # parser.set_defaults({"trainer.gradient_clip_val": 5, "trainer.gradient_clip_algorithm":"norm"})

        # 添加早停策略默认参数
        parser.add_lightning_class_args(EarlyStopping, "early_stopping")
        parser.set_defaults({
            "early_stopping.monitor": "val/loss",
            "early_stopping.min_delta": 0.01,
            "early_stopping.patience": 10,
            "early_stopping.mode": "min",
        })

        # 添加模型保存默认参数
        parser.add_lightning_class_args(ModelCheckpoint, "model_checkpoint")
        model_checkpoint_defaults = {
            "model_checkpoint.filename": "epoch{epoch}_valid_loss{val/loss:.4f}",
            "model_checkpoint.monitor": "val/loss",
            "model_checkpoint.mode": "min",
            "model_checkpoint.every_n_epochs": 1,
            "model_checkpoint.save_top_k": 5,
            "model_checkpoint.auto_insert_metric_name": False,
            "model_checkpoint.save_last": True
        }
        parser.set_defaults(model_checkpoint_defaults)

        # RichProgressBar
        parser.add_lightning_class_args(RichProgressBar, nested_key='progress_bar')
        parser.set_defaults({
            "progress_bar.console_kwargs": {
                "force_terminal": True,
                "no_color": True,  # 去除颜色，节省nohup保存下来的log文件的大小
                "width": 200,  # 设置足够的宽度，防止将进度条分成两行
            }
        })

        # LearningRateMonitor
        parser.add_lightning_class_args(LearningRateMonitor, "learning_rate_monitor")
        learning_rate_monitor_defaults = {
            "learning_rate_monitor.logging_interval": "epoch",
        }
        parser.set_defaults(learning_rate_monitor_defaults)

        # 设置profiler寻找代码最耗时的位置。去除下面的注释把profiler打开
        # from pytorch_lightning.profiler import SimpleProfiler, AdvancedProfiler
        # parser.set_defaults({"trainer.profiler": lazy_instance(AdvancedProfiler, filename="profiler")})
        # parser.set_defaults({"trainer.max_epochs": 1, "trainer.limit_train_batches": 100, "trainer.limit_val_batches": 100})

    def before_fit(self):
        # 训练开始前，会被执行。下面代码的功能是如果是从last.ckpt恢复训练，则输出到同一个目录（如version_10）;否则，输出到logs/{model_name}/version_NEW
        resume_from_checkpoint: str = self.config['fit']['ckpt_path']
        if resume_from_checkpoint is not None and resume_from_checkpoint.endswith('last.ckpt'):
            # 如果是从last.ckpt恢复训练，则输出到同一个目录
            # resume_from_checkpoint example: /home/zhangsan/logs/MyModel/version_29/checkpoints/last.ckpt
            resume_from_checkpoint = os.path.normpath(resume_from_checkpoint)
            splits = resume_from_checkpoint.split(os.path.sep)
            version = int(splits[-3].replace('version_', ''))
            save_dir = os.path.sep.join(splits[:-3])
            self.trainer.logger = TensorBoardLogger(save_dir=save_dir, name="", version=version, default_hp_metric=False)
        else:
            model_name = type(self.model).__name__
            self.trainer.logger = TensorBoardLogger('logs/', name=model_name, default_hp_metric=False)

    def before_test(self):
        # 测试开始前，会被执行。下面代码实现的功能是将测试时的log目录设置为/home/zhangsan/logs/MyModel/version_X/epochN/version_Y
        torch.set_num_interop_threads(5)
        torch.set_num_threads(5)
        if self.config['test']['ckpt_path'] != None:
            ckpt_path = self.config['test']['ckpt_path']
        else:
            raise Exception('You should give --ckpt_path if you want to test')
        epoch = os.path.basename(ckpt_path).split('_')[0]
        write_dir = os.path.dirname(os.path.dirname(ckpt_path))
        exp_save_path = os.path.normpath(write_dir + '/' + epoch)

        import time
        # add 10 seconds for threads to simultaneously detect the next version
        self.trainer.logger = TensorBoardLogger(exp_save_path, name='', default_hp_metric=False)
        time.sleep(10)

    def after_test(self):
        # 测试结束之后，会被执行。下面代码实现的功能是将测试时生成的tensorboard log文件删除，防止tensorboard看着混乱。
        if not self.trainer.is_global_zero:
            return
        import fnmatch
        files = fnmatch.filter(os.listdir(self.trainer.log_dir), 'events.out.tfevents.*')
        for f in files:
            os.remove(self.trainer.log_dir + '/' + f)
            print('tensorboard log file for test is removed: ' + self.trainer.log_dir + '/' + f)


if __name__ == '__main__':
    cli = MyCLI(
        MyModel,
        MyDataModule,
        seed_everything_default=2,  # 可以修改为自己想要的值
        save_config_callback=SaveConfigCallback,
        save_config_kwargs={'overwrite': True},
        # parser_kwargs={"parser_mode": "omegaconf"},
    )
