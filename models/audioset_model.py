# coding=utf-8
# Copyright 2020 Yuan-Hang Zhang.
#
from .audioset_dataset import AudioSetDataset
from .rnn import GRU

from .lr_finder import BatchExponentialLR, plot_lr

from argparse import ArgumentParser

import torch
from torch.nn import functional as F
from torch.utils.data import DataLoader

import pytorch_lightning as pl

import sys


LR_TEST_MAX_LR = 0.1
LR_TEST_STEPS = 600


class AudioSet(pl.LightningModule):
    
    def __init__(self, hparams):
        super(AudioSet, self).__init__()
        self.hparams = hparams
        
        self.audio = GRU(200, self.hparams.num_hidden, 2, 527, self.hparams.num_fc_layers, dropout=True)

        self.history = {'lr': [], 'loss': []}

    def forward(self, x):
        # temporal max-pooling
        return torch.max(self.audio(x), dim=1)[0]
    
    def bce_loss(self, y_hat, y):
        return F.binary_cross_entropy_with_logits(y_hat, y)
    
    def training_step(self, batch, batch_idx):
        x, y = batch['audio'], batch['label']
        y_hat = self.forward(x)
        loss = self.bce_loss(y_hat, y)

        max_class = torch.argmax(y_hat, dim=-1)
        # Top-1 Acc
        correct = torch.gather(y, 1, max_class.view(-1, 1)).view(-1)
        acc = torch.sum(correct).item() / x.size(0)
        
        if self.hparams.test_lr:
            if len(self.history['lr']) == LR_TEST_STEPS:
                plot_lr(self.history)
                print ('Saved LR-loss plot.')
                sys.exit(0)
            else:
                lr = self.lr_test.get_lr()[0]
                self.history['lr'].append(lr)
                if batch_idx != 0: # smoothing
                    self.history['loss'].append(0.05 * loss.item() + 0.95 * self.history['loss'][-1])
                else:
                    self.history['loss'].append(loss.item())

        return {
            'loss': loss,
            'progress_bar': {'loss': loss, 'train_acc': acc},
            'log': {'loss': loss, 'train_acc': acc}
        }
    
    def on_batch_end(self):
        if self.hparams.test_lr:
            self.lr_test.step()
        if self.hparams.scheduler == 'cyclic':
            self.cyclic_scheduler.step()

    def validation_step(self, batch, batch_idx):
        x, y = batch['audio'], batch['label']
        y_hat = self.forward(x)
        loss = self.bce_loss(y_hat, y)

        max_class = torch.argmax(y_hat, dim=-1)
        correct = torch.gather(y, 1, max_class.view(-1, 1)).view(-1)

        return {
            'val_loss': loss,
            'correct': correct
        }

    def validation_end(self, outputs):
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()
        all_corrects = torch.cat([x['correct'] for x in outputs])
        val_acc = torch.sum(all_corrects).item() / len(all_corrects)

        return {
            'val_loss': avg_loss,
            'progress_bar': {
                'val_loss': avg_loss,
                'val_acc': val_acc,
            },
            'log': {
                'val_loss': avg_loss,
                'val_acc': val_acc,
            }
        }

    def configure_optimizers(self):
        if self.hparams.optimizer == 'adam':
            optimizer = torch.optim.Adam(self.parameters(),
                                         lr=self.hparams.learning_rate,
                                         weight_decay=1e-4)
        elif self.hparams.optimizer == 'sgd':
            optimizer = torch.optim.SGD(self.parameters(),
                                        lr=self.hparams.learning_rate,
                                        momentum=0.9, weight_decay=5e-4)
        if self.hparams.test_lr:
            self.lr_test = BatchExponentialLR(optimizer, LR_TEST_MAX_LR, LR_TEST_STEPS)
            return optimizer
        else:
            if self.hparams.scheduler == 'cyclic':
                self.cyclic_scheduler = torch.optim.lr_scheduler.CyclicLR(optimizer, self.hparams.min_lr, self.hparams.learning_rate, step_size_up=480, cycle_momentum=self.hparams.optimizer == 'sgd')
                return optimizer
            elif self.hparams.scheduler == 'exp':
                scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, self.hparams.decay_factor)
                return [optimizer], [scheduler]
            elif self.hparams.scheduler == 'plateau':
                scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, factor=self.hparams.decay_factor, patience=3, verbose=True, min_lr=1e-6)
                return [optimizer], [scheduler]
    
    @pl.data_loader
    def train_dataloader(self):
        dataset = AudioSetDataset('train', self.hparams.dataset_path, self.hparams.window)
        if self.hparams.distributed:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
            return DataLoader(dataset, batch_size=self.hparams.batch_size, num_workers=self.hparams.workers, pin_memory=True, sampler=dist_sampler)
        else:
            return DataLoader(dataset, batch_size=self.hparams.batch_size, shuffle=True, num_workers=self.hparams.workers, pin_memory=True)

    @pl.data_loader
    def val_dataloader(self):
        dataset = AudioSetDataset('val', self.hparams.dataset_path, self.hparams.window)
        if self.hparams.distributed:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
            return DataLoader(dataset, batch_size=self.hparams.batch_size, num_workers=self.hparams.workers, pin_memory=True, sampler=dist_sampler)
        else:
            return DataLoader(dataset, batch_size=self.hparams.batch_size, shuffle=False, num_workers=self.hparams.workers, pin_memory=True)

    @staticmethod
    def add_model_specific_args(parent_parser):
        """
        Specify the hyperparams for this LightningModule
        """
        # MODEL specific
        parser = ArgumentParser(parents=[parent_parser])

        parser.add_argument('--learning_rate', default=0.3, type=float)
        parser.add_argument('--min_lr', default=1e-3, type=float)
        parser.add_argument('--decay_factor', default=0.5, type=float)
        parser.add_argument('--batch_size', default=128, type=int)
        parser.add_argument('--optimizer', default='adam', type=str)
        parser.add_argument('--scheduler', default='plateau', type=str)

        parser.add_argument('--test_lr', action='store_true', default=False)

        parser.add_argument('--num_fc_layers', default=2, type=int)
        parser.add_argument('--num_hidden', default=256, type=int)
        parser.add_argument('--window', default=32, type=int)

        # training specific (for this model)
        parser.add_argument('--distributed', action='store_true', default=False)
        parser.add_argument('--dataset_path', default='/data/f/zhangyuanhang/Aff-Wild2/AudioSet_16k', type=str)
        parser.add_argument('--checkpoint_path', default='./audioset', type=str)
        parser.add_argument('--workers', default=8, type=int)
        parser.add_argument('--max_nb_epochs', default=80, type=int)

        return parser
