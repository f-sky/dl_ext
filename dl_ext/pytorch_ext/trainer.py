import os
import time
from enum import IntEnum

from tensorboardX import SummaryWriter
from termcolor import colored
from torch import nn
from torch.nn import DataParallel
from torch.nn.parallel import DistributedDataParallel
from torch.optim import Adam
from torch.optim.lr_scheduler import _LRScheduler
from torch.optim.optimizer import Optimizer
from torch.utils.data import DataLoader, DistributedSampler
from tqdm import tqdm
from .sampler import OrderedDistributedSampler
from .dist import *
from .optim import OneCycleScheduler
from ..average_meter import AverageMeter


def to_cuda(x):
    if hasattr(x, 'cuda'):
        return x.cuda(device=get_rank())
    elif isinstance(x, (list, tuple)):
        return [to_cuda(xi) for xi in x]
    elif isinstance(x, dict):
        return {k: to_cuda(v) for k, v in x.items()}


def to_cpu(x):
    if hasattr(x, 'cpu'):
        return x.cpu()
    elif isinstance(x, (list, tuple)):
        return [to_cpu(xi) for xi in x]
    elif isinstance(x, dict):
        return {k: to_cpu(v) for k, v in x.items()}


def batch_gpu(batch):
    x, y = batch
    return to_cuda(x), to_cuda(y)


def batch_cpu(batch):
    x, y = batch
    return to_cpu(x), to_cpu(y)


class TrainerState(IntEnum):
    BASE = 1
    PARALLEL = 2
    DISTRIBUTEDPARALLEL = 3


class BaseTrainer:

    def __init__(self, model: nn.Module, train_dl: DataLoader, valid_dl: DataLoader, num_epochs: int,
                 loss_function: callable, optimizer: Optimizer = None, scheduler: _LRScheduler = None,
                 output_dir: str = 'models', max_lr: float = 1e-2, save_every: bool = False,
                 metric_functions: dict = None):
        self.loss_function = loss_function
        self.train_dl = train_dl
        self.valid_dl = valid_dl
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.num_epochs = num_epochs
        self.begin_epoch = 0
        self.max_lr = max_lr
        self.save_every = save_every
        self.state = TrainerState.BASE
        if optimizer is None:
            optimizer = Adam(model.parameters(), lr=max_lr)
        self.optimizer = optimizer
        if scheduler is None:
            scheduler = OneCycleScheduler(self.optimizer, self.max_lr,
                                          total_steps=len(train_dl) * num_epochs)
        self.scheduler = scheduler
        self.model: nn.Module = model
        if metric_functions is None:
            metric_functions = {}
        self.metric_functions = metric_functions
        self.tb_writer = SummaryWriter(output_dir, flush_secs=20)
        self.global_steps = 0
        self.best_val_loss = 100000

    def train(self, epoch):
        loss_meter = AverageMeter()
        metric_ams = {}
        for metric in self.metric_functions.keys():
            metric_ams[metric] = AverageMeter()
        self.model.train()
        bar = tqdm(self.train_dl, leave=False) if is_main_process() else self.train_dl
        for batch in bar:
            self.optimizer.zero_grad()
            x, y = batch_gpu(batch)
            output = self.model(x)
            loss = self.loss_function(output, y)
            loss = loss.mean()
            loss.backward()
            self.optimizer.step()
            if isinstance(self.scheduler, OneCycleScheduler):
                self.scheduler.step()
            # record and plot loss and metrics
            reduced_loss = reduce_loss(loss)
            reduced_output = torch.cat(all_gather(output), 0)
            reduced_y = torch.cat(all_gather(y), 0)
            if is_main_process():
                loss_meter.update(reduced_loss.item())
                lr = self.optimizer.param_groups[0]['lr']
                self.tb_writer.add_scalar('train/loss', reduced_loss.item(), self.global_steps)
                self.tb_writer.add_scalar('train/lr', lr, self.global_steps)
                metrics = {}
                for metric, f in self.metric_functions.items():
                    s = f(reduced_output, reduced_y).mean().item()
                    metric_ams[metric].update(s)
                    metrics[metric] = metric_ams[metric].avg
                    self.tb_writer.add_scalar(f'train/{metric}', s, self.global_steps)
                bar_vals = {'epoch': epoch, 'phase': 'train', 'loss': loss_meter.avg, 'lr': lr}
                for k, v in metrics.items():
                    bar_vals[k] = v
                bar.set_postfix(bar_vals)
            self.global_steps += 1
        if is_main_process():
            metric_msgs = ['epoch %d, train, loss %.4f' % (epoch, loss_meter.avg)]
            for metric, v in metric_ams.items():
                metric_msgs.append('%s %.4f' % (metric, v.avg))
            s = ', '.join(metric_msgs)
            print(s)
        if not isinstance(self.scheduler, OneCycleScheduler):
            self.scheduler.step()

    @torch.no_grad()
    def val(self, epoch):
        loss_meter = AverageMeter()
        metric_ams = {}
        for metric in self.metric_functions.keys():
            metric_ams[metric] = AverageMeter()
        self.model.eval()
        bar = tqdm(self.valid_dl, leave=False) if is_main_process() else self.valid_dl
        for batch in bar:
            x, y = batch_gpu(batch)
            output = self.model(x)
            loss = self.loss_function(output, y)
            loss = loss.mean()
            reduced_loss = reduce_loss(loss)
            reduced_output = torch.cat(all_gather(output), 0)
            reduced_y = torch.cat(all_gather(y), 0)
            if is_main_process():
                loss_meter.update(reduced_loss.item())
                metrics = {}
                for metric, f in self.metric_functions.items():
                    s = f(reduced_output, reduced_y).mean().item()
                    metric_ams[metric].update(s)
                    metrics[metric] = metric_ams[metric].avg
                bar_vals = {'epoch': epoch, 'phase': 'val', 'loss': loss_meter.avg}
                for k, v in metrics.items():
                    bar_vals[k] = v
                bar.set_postfix(bar_vals)
        if is_main_process():
            metric_msgs = ['epoch %d, val, loss %.4f' % (epoch, loss_meter.avg)]
            for metric, v in metric_ams.items():
                metric_msgs.append('%s %.4f' % (metric, v.avg))
            s = ', '.join(metric_msgs)
            print(s)
            self.tb_writer.add_scalar('val/loss', loss_meter.avg, epoch)
            for metric, s in metric_ams.items():
                self.tb_writer.add_scalar(f'val/{metric}', s.avg, epoch)
            return loss_meter.avg

    def fit(self):
        os.makedirs(self.output_dir, exist_ok=True)
        num_epochs = self.num_epochs
        begin = time.time()
        for epoch in range(self.begin_epoch, num_epochs):
            self.train(epoch)
            synchronize()
            val_loss = self.val(epoch)
            synchronize()
            if is_main_process():
                if self.save_every:
                    self.save(epoch)
                elif val_loss < self.best_val_loss:
                    print(colored('Better model found at epoch %d with val_loss %.4f.' % (epoch, val_loss), 'red'))
                    self.best_val_loss = val_loss
                    self.save(epoch)
            synchronize()
        if is_main_process():
            print('Training finished. Total time %d s' % (time.time() - begin))

    @torch.no_grad()
    def get_preds(self, dataset='valid', with_target=False):
        if get_world_size() > 1:
            return self.get_preds_dist(dataset, with_target)
        self.model.eval()
        assert dataset in ['train', 'valid']
        if dataset == 'train':
            bar = tqdm(self.train_dl) if is_main_process() else self.train_dl
        else:
            bar = tqdm(self.valid_dl) if is_main_process() else self.valid_dl
        outputs = []
        targets = []
        for batch in bar:
            x, y = batch_gpu(batch)
            output = self.model(x)
            output = to_cpu(output)
            outputs.append(output)
            if with_target:
                targets.append(to_cpu(y))
        outputs = torch.cat(outputs)
        if with_target:
            targets = torch.cat(targets)
            return outputs, targets
        else:
            return outputs

    @torch.no_grad()
    def get_preds_dist(self, dataset='valid', with_target=False):
        old_valid_dl = self.valid_dl
        valid_sampler = OrderedDistributedSampler(self.valid_dl.dataset, get_world_size(), rank=get_rank())
        self.valid_dl = DataLoader(self.valid_dl.dataset, self.valid_dl.batch_size, shuffle=False,
                                   sampler=valid_sampler, num_workers=self.valid_dl.num_workers,
                                   collate_fn=self.valid_dl.collate_fn, pin_memory=self.valid_dl.pin_memory,
                                   timeout=self.valid_dl.timeout, worker_init_fn=self.valid_dl.worker_init_fn)
        self.model.eval()
        if dataset == 'train':
            bar = tqdm(self.train_dl) if is_main_process() else self.train_dl
        else:
            bar = tqdm(self.valid_dl) if is_main_process() else self.valid_dl
        outputs = []
        targets = []
        for batch in bar:
            x, y = batch_gpu(batch)
            output = self.model(x)
            output = to_cpu(output)
            outputs.append(output)
            if with_target:
                targets.append(to_cpu(y))
        self.valid_dl = old_valid_dl
        outputs = torch.cat(outputs)
        all_outputs = all_gather(outputs)
        if with_target:
            targets = torch.cat(targets)
            all_targets = all_gather(targets)
        if not is_main_process():
            return
        all_outputs = torch.cat(all_outputs, dim=0).cpu()[:len(self.valid_dl.dataset)]
        if with_target:
            all_targets = torch.cat(all_targets, dim=0).cpu()[:len(self.valid_dl.dataset)]
            return all_outputs, all_targets
        else:
            return all_outputs

    def to_base(self):
        if self.state == TrainerState.BASE:
            return
        elif self.state == TrainerState.PARALLEL:
            self.model = self.model.module
        else:
            self.model = self.model.module
            self.train_dl = self.old_train_dl
            self.valid_dl = self.old_valid_dl

    def to_parallel(self):
        assert self.state == TrainerState.BASE
        devices = os.environ['CUDA_VISIBLE_DEVICES']
        print('visible devices', devices)
        self.model = DataParallel(self.model)

    def to_distributed(self):
        assert dist.is_available() and dist.is_initialized()
        local_rank = dist.get_rank()
        self.model = DistributedDataParallel(self.model, [local_rank],
                                             output_device=local_rank,
                                             broadcast_buffers=False)
        self.old_train_dl = self.train_dl
        train_sampler = DistributedSampler(self.train_dl.dataset, shuffle=True)
        new_train_dl = DataLoader(self.train_dl.dataset, self.train_dl.batch_size, shuffle=False,
                                  sampler=train_sampler, num_workers=self.train_dl.num_workers,
                                  collate_fn=self.train_dl.collate_fn, pin_memory=self.train_dl.pin_memory,
                                  timeout=self.train_dl.timeout, worker_init_fn=self.train_dl.worker_init_fn)
        # new_train_dl = self.train_dl.new(shuffle=False, sampler=train_sampler)
        self.train_dl = new_train_dl
        self.old_valid_dl = self.valid_dl
        valid_sampler = DistributedSampler(self.valid_dl.dataset, shuffle=False)
        new_valid_dl = DataLoader(self.valid_dl.dataset, self.valid_dl.batch_size, shuffle=False,
                                  sampler=valid_sampler, num_workers=self.valid_dl.num_workers,
                                  collate_fn=self.valid_dl.collate_fn, pin_memory=self.valid_dl.pin_memory,
                                  timeout=self.valid_dl.timeout, worker_init_fn=self.valid_dl.worker_init_fn)
        self.valid_dl = new_valid_dl

    def save(self, epoch):
        name = os.path.join(self.output_dir, str(epoch) + '.pth')
        net_sd = self.model.module.state_dict() if hasattr(self.model, 'module') else self.model.state_dict()
        d = {'model': net_sd,
             'optimizer': self.optimizer.state_dict(),
             'scheduler': self.scheduler.state_dict(),
             'epoch': epoch,
             'best_val_loss': self.best_val_loss}
        torch.save(d, name)

    def load(self, name):
        name = os.path.join(self.output_dir, name + '.pth')
        d = torch.load(name)
        net_sd = d['model']
        if hasattr(self.model, 'module'):
            self.model.module.load_state_dict(net_sd)
        else:
            self.model.load_state_dict(net_sd)
        self.optimizer.load_state_dict(d['optimizer'])
        self.scheduler.load_state_dict(d['scheduler'])
        self.begin_epoch = d['epoch']
        self.best_val_loss = d['best_val_loss']
