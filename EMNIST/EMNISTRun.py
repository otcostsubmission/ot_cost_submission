from config import ROOT_DIR
import pandas as pd
import torch
import torch.nn as nn
import sys
import numpy as np
sys.path.append(f'{ROOT_DIR}/code/helper')
import pipeline as pp
import graph_results as gr
import importlib
importlib.reload(pp)
importlib.reload(gr)
import pickle
from torch.optim.lr_scheduler import ExponentialLR

EPOCHS = 100
BATCH_SIZE = 5000
RUNS = 10000
DATASET = 'EMNIST'
METRIC_TEST = 'Accuracy'
LEARNING_RATE = 5e-3
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


##FF model
class LeNet5(nn.Module):
    def __init__(self, CLASSES):
        super(LeNet5, self).__init__()
        self.layer1 = nn.Sequential(
            nn.Conv2d(1, 6, kernel_size=5, stride=1, padding=0),
            nn.BatchNorm2d(6),
            nn.ReLU(),
            nn.MaxPool2d(kernel_size = 2, stride = 2))
        self.layer2 = nn.Sequential(
            nn.Conv2d(6, 16, kernel_size=5, stride=1, padding=0),
            nn.BatchNorm2d(16),
            nn.ReLU(),
            nn.MaxPool2d(kernel_size = 2, stride = 2))
        self.fc = nn.Linear(256, 120)
        self.relu = nn.ReLU()
        self.fc1 = nn.Linear(120, 84)
        self.relu1 = nn.ReLU()
        self.fc2 = nn.Linear(84, CLASSES)
        self.softmax = torch.nn.Softmax(dim = 1)
        
    def forward(self, x):
        out = self.layer1(x)
        out = self.layer2(out)
        out = out.reshape(out.size(0), -1)
        out = self.fc(out)
        out = self.relu(out)
        out = self.fc1(out)
        out = self.relu1(out)
        out = self.fc2(out)
        out = self.softmax(out)
        return out
        
def createModel(mp):
    if mp.SINGLE:
        CLASSES = mp.SINGLE_CLASS
    else:
        CLASSES = mp.MSL_CLASS
    print(CLASSES)
    model = LeNet5(CLASSES)
    model.to(DEVICE)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr = LEARNING_RATE)
    lr_scheduler = ExponentialLR(optimizer, gamma=0.9)
    return model, criterion, optimizer, lr_scheduler


def sample_per_class(labels, class_size = 500):
  df = pd.DataFrame({'labels': labels})
  df_stratified = df.groupby('labels').apply(lambda x: x.sample(class_size, replace=False))
  ind = df_stratified.index.get_level_values(1)
  return ind

def loadData(dataset, cost):
    ##load data
    data = np.load(f'{ROOT_DIR}/data/{DATASET}/data_{dataset}_{cost:.2f}.npz')
    ##get X and label
    X = data['data']
    y = data['labels']
    class_size = 100
    ind = sample_per_class(y, class_size)
    X_sample =  X[ind]
    y_sample = y[ind]
    return X_sample, y_sample

def run_model_for_cost(inputs):
    c, loadData, DATASET, METRIC_TEST, BATCH_SIZE, EPOCHS, DEVICE, RUNS = inputs
    mp = pp.ModelPipeline(c, loadData, DATASET, METRIC_TEST, BATCH_SIZE, EPOCHS, DEVICE, RUNS)
    mp.set_functions(createModel(mp))
    return mp.run_model_for_cost()


def main():
     ##run model on datasets
    costs = [0.11, 0.19, 0.25, 0.34, 0.39]
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