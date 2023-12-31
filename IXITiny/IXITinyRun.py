from config import ROOT_DIR
import pandas as pd
import torch
import torch.nn as nn
import sys
import os
import numpy as np
sys.path.append(f'{ROOT_DIR}/code/helper')
import trainers as tr
import pipeline as pp
import graph_results as gr
import importlib
importlib.reload(tr)
importlib.reload(pp)
importlib.reload(gr)
import pickle
from unet import UNet
import torch.nn.functional as F
from torch.optim.lr_scheduler import ExponentialLR

EPOCHS = 500
BATCH_SIZE = 16
RUNS = 1
DATASET = 'IXITiny'
METRIC_TEST = 'DICE'
LEARNING_RATE = 5e-3
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
CHANNELS_DIMENSION = 1
SPATIAL_DIMENSIONS = 2, 3, 4


class UNetClassifier(nn.Module):
    def __init__(self):
        super(UNetClassifier, self).__init__()
        self.CHANNELS_DIMENSION = 1
        self.SPATIAL_DIMENSIONS = 2, 3, 4

        unet = UNet(
            in_channels=1,
            out_classes=2,
            dimensions=3,
            num_encoding_blocks=3,
            out_channels_first_layer=8,
            normalization='batch',
            upsampling_type='linear',
            padding=True,
            activation='PReLU',
        )

        # Make all other parameters untrainable
        for name, param in unet.named_parameters():
            if 'classifier' not in name:
                param.requires_grad = False

        self.classifier = unet.classifier
        self.initialize_weights()
        del unet


    def forward(self, x):
        #unet has been removed as we save the representation up until then
        logits = self.classifier(x)
        probabilities = F.softmax(logits, dim=self.CHANNELS_DIMENSION)
        return probabilities
    
    def initialize_weights(self):
        if isinstance(self.classifier, nn.Conv3d):
            nn.init.xavier_normal_(self.classifier.weight.data)
            if self.classifier.bias is not None:
                nn.init.constant_(self.classifier.bias.data, 0)

def createModel():
    model = UNetClassifier()
    model = model.to(DEVICE)
    criterion = tr.get_dice_loss
    optimizer = torch.optim.Adam(model.parameters(), lr = LEARNING_RATE)
    lr_scheduler = ExponentialLR(optimizer, gamma=0.9)
    return model, criterion, optimizer, lr_scheduler

def loadData(dataset, cost):
    sites = {0.03: [['Guys'], ['HH']],
             0.24: [['IOP'], ['Guys']],
             0.28: [['IOP'], ['HH']]}
    site_names = sites[cost][dataset-1]

    image_dir = os.path.join(ROOT_DIR, 'data/IXITiny/representation')
    label_dir = os.path.join(ROOT_DIR, 'data/IXITiny/label')
    image_files = []
    label_files = []
    for name in site_names:
            image_files.extend([f'{image_dir}/{file}' for file in os.listdir(image_dir) if name in file])
            label_files.extend([f'{label_dir}/{file}'  for file in os.listdir(label_dir) if name in file])
    return image_files, label_files


def run_model_for_cost(inputs):
    c, loadData, DATASET, METRIC_TEST, BATCH_SIZE, EPOCHS, DEVICE, RUNS = inputs
    mp = pp.ModelPipeline(c, loadData, DATASET, METRIC_TEST, BATCH_SIZE, EPOCHS, DEVICE, RUNS)
    mp.set_functions(createModel())
    return mp.run_model_for_cost()


def main():
     ##run model on datasets
    costs = [0.03, 0.24, 0.28]
    inputs = [(c, loadData, DATASET, METRIC_TEST, BATCH_SIZE, EPOCHS, DEVICE, RUNS) for c in costs]
    results = []
    for input in inputs:
        results.append(run_model_for_cost(input))

    losses = {}
    metrics_all = pd.DataFrame()
    for c, loss, metrics in results:
        losses[c] = loss
        metrics_all = pd.concat([metrics_all, metrics], axis=0)
    metrics_all.reset_index(inplace = True, drop = True)
    losses_df, test_losses_df = pp.loss_dictionary_to_dataframe(losses, costs, RUNS)
    

    ##Save results
    path_save = f'{ROOT_DIR}/results/{DATASET}'
    cost = f'{costs[0]}-{costs[-1]}'
    metrics_all.to_csv(f'{path_save}/{METRIC_TEST}_{cost}.csv', index=False)
    test_losses_df.to_csv(f'{path_save}/losses_{cost}.csv', index=False)
    with open(f'{path_save}/losses.pkl', 'wb') as f:
        pickle.dump(losses_df, f)

    
    ##Save graph
    save = True
    gr.grapher(DATASET, metrics_all, METRIC_TEST, cost, save)
    gr.grapher(DATASET, test_losses_df, 'Loss', cost, save)
    gr.grapher_losses(DATASET, losses_df, costs, save)

if __name__ == '__main__':
    main()