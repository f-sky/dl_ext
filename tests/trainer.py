import argparse
import os

import torch
import torchvision
import torchvision.transforms as transforms
from torch import nn
from torch import optim
from torch.nn import init
from torch.utils.data import DataLoader
from torchvision.models import resnet18, ResNet

from dl_ext.pytorch_ext import OneCycleScheduler
from dl_ext.pytorch_ext.trainer import BaseTrainer, is_main_process, get_world_size
from dl_ext.vision_ext.transforms import imagenet_normalize

parser = argparse.ArgumentParser()
parser.add_argument('--epochs', type=int, default=10)
parser.add_argument('--local_rank', type=int, default=0)
parser.add_argument('--lr', type=float, default=1e-2)
parser.add_argument('--logdir', default='log')

args = parser.parse_args()


def build_dataloaders():
    train_transform = transforms.Compose([transforms.ToTensor(),
                                          imagenet_normalize])
    val_transform = transforms.Compose([transforms.ToTensor(),
                                        imagenet_normalize])
    trainset = torchvision.datasets.CIFAR10(root=os.path.expanduser('~/Datasets/cifar10'),
                                            train=True,
                                            download=True, transform=train_transform)
    trainloader = DataLoader(trainset, batch_size=128,
                             shuffle=False, num_workers=0)

    testset = torchvision.datasets.CIFAR10(root=os.path.expanduser('~/Datasets/cifar10'),
                                           train=False,
                                           download=True, transform=val_transform)
    testloader = DataLoader(testset, batch_size=1024,
                            shuffle=False, num_workers=0)
    return trainloader, testloader


def build_model():
    torch.manual_seed(0)
    model: ResNet = resnet18(pretrained=True)
    model.fc = nn.Linear(model.fc.in_features, 10)
    init.constant_(model.fc.weight,0.001)
    init.constant_(model.fc.bias,0)
    model.cuda()
    return model


# def build_optim(model, trainloader):
#     # if args.one_cycle:
#     optimizer = optim.Adam(model.parameters(), lr=args.lr)
#     scheduler = OneCycleScheduler(optimizer, max_lr=args.lr,
#                                   total_steps=len(trainloader) * args.epochs,
#                                   cycle_momentum=True)
#     # else:
#     #         optimizer = optim.Adam(model.parameters(), lr=args.lr)
#     #         scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=20)
#     return optimizer, scheduler


def accuracy(output, y):
    return (output.argmax(-1) == y).sum().float() / y.shape[0]


def main():
    num_gpus = int(os.environ["WORLD_SIZE"]) if "WORLD_SIZE" in os.environ else 1
    if num_gpus > 1:
        torch.cuda.set_device(args.local_rank)
        torch.distributed.init_process_group(
            backend="nccl", init_method="env://"
        )
    trainloader, testloader = build_dataloaders()
    model = build_model()
    criterion = nn.CrossEntropyLoss()
    trainer = BaseTrainer(model, trainloader, testloader,
                          args.epochs, criterion,
                          metric_functions={'accuracy': accuracy},
                          save_every=True,
                          max_lr=1e-2)
    if num_gpus > 1:
        trainer.to_distributed()
    # train
    # trainer.find_lr(suggestion=True)
    trainer.fit()
    # io model and evaluate
    # trainer.io('2')
    # results = trainer.get_preds(with_target=True)
    # if is_main_process():
    #     preds, tgts = results
    #     tgts = []
    #     for x, y in testloader.dataset:
    #         tgts.append(y)
    #     tgts = torch.tensor(tgts).long()
    #     print((preds.argmax(1) == tgts).sum().item() / tgts.shape[0])


if __name__ == '__main__':
    main()
