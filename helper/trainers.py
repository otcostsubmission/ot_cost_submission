import torch
import torch.nn as nn
import torch.nn.functional as F
import copy
from sklearn import metrics
import numpy as np
import warnings
# Suppress the specific LR warning that is non-issue
warnings.filterwarnings("ignore", "Seems like `optimizer.step()` has been overridden after learning rate scheduler initialization.")


DATASET_TYPES_NUMERIC = {'Synthetic', 'Credit', 'Weather', 'IXITiny'}
DATASET_TYPES_CATEGORICAL = {'CIFAR', 'EMNIST'}
FED_EPOCH = 2

def to_device(data, device):
    return data.to(device)


class MetricsFactory:
    @staticmethod
    def get_metric(metric_name):
        metric_mapping = {
            'AUC': metrics.roc_auc_score,
            'AUPRC': metrics.average_precision_score,
            'R2': metrics.r2_score,
            'Accuracy': metrics.accuracy_score,
            'DICE': get_dice_score
        }
        return metric_mapping[metric_name]


class ModelTrainer:
    def __init__(self, dataset, model, optimizer, criterion, lr_scheduler, device, patience=5):
        self.dataset = dataset
        self.loss_function = self._setup_loss_function()
        self.device = device
        self.model = model.to(self.device)
        self.optimizer = optimizer
        self.criterion = criterion
        self.lr_scheduler = lr_scheduler
        self.best_loss = float('inf')
        self.best_model = copy.deepcopy(model)
        self.best_model = self.best_model.to(self.device)
        self.train_losses = []
        self.val_losses = []
        self.test_losses = []
        self.patience = patience
        self.early_stopping_counter = 0
        self.stopping = False

    def fit(self, train_dataloader, val_dataloader):
        self.model.train()
        train_loss = 0
        for x, y in train_dataloader:
            x, y = to_device(x, self.device), to_device(y, self.device)
            loss = self._train_step(x, y)
            train_loss += loss.item()
        train_loss /= len(train_dataloader)
        self.train_losses.append(train_loss)

        self.model.eval()
        val_loss = 0
        with torch.no_grad():
            for x, y in val_dataloader:
                x, y = to_device(x, self.device), to_device(y, self.device)
                loss = self._validate_step(x, y)
                val_loss += loss.item()
        val_loss /= len(val_dataloader)
        self.val_losses.append(val_loss)

        #print(f'Train Loss = {train_loss}, Val Loss = {val_loss}')

        if val_loss < self.best_loss:
            self.best_loss = val_loss
            self.best_model = copy.deepcopy(self.model)
            self.early_stopping_counter = 0

        elif len(self.val_losses) >= 10 and val_loss > min(self.val_losses[-3:]):
                self.early_stopping_counter += 1
                if self.early_stopping_counter >= self.patience:
                    print("Early stopping")
                    self.stopping = True
        else:
            self.early_stopping_counter = 0
            
        self.lr_scheduler.step()


    def test(self, dataloader, metric_name):
        metric_function = MetricsFactory.get_metric(metric_name)
        all_outputs = []
        all_labels = []
        self.best_model.eval()
        test_loss = 0
        with torch.no_grad():
            for x, y in dataloader:
                x, y = to_device(x, self.device), to_device(y, self.device)
                outputs = self.best_model(x.float())
                loss = self._compute_loss(outputs, y)
                test_loss += loss.item() 
                all_outputs.extend(outputs.cpu().numpy())
                all_labels.extend(y.cpu().numpy())
        
        test_loss /= len(dataloader)
        self.test_losses.append(test_loss)
        all_outputs = np.array(all_outputs)
        if self.dataset in DATASET_TYPES_CATEGORICAL:
            all_outputs = all_outputs.argmax(axis = 1) 
        all_labels = np.array(all_labels)
        if self.dataset == 'IXITiny':
            all_labels = torch.tensor(all_labels, dtype=torch.float32)
            all_outputs = torch.tensor(all_outputs, dtype=torch.float32)
        evaluation = metric_function(all_labels, all_outputs)
        return evaluation

    def _train_step(self, x, y):
        outputs = self.model(x.float())
        loss = self._compute_loss(outputs, y)
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step() 
        return loss

    def _validate_step(self, x, y):
        outputs = self.model(x.float())
        val_loss = self._compute_loss(outputs, y)
        return val_loss

    def _compute_loss(self, outputs, y):
        return self.loss_function(outputs, y)

    def _setup_loss_function(self):
        if self.dataset in DATASET_TYPES_NUMERIC:
            return lambda outputs, y: self.criterion(outputs.squeeze(), y.float().squeeze())
        elif self.dataset in DATASET_TYPES_CATEGORICAL:
            return lambda outputs, y: self.criterion(outputs.squeeze(), y.long().squeeze())


class TransferModelTrainer(ModelTrainer):
    def __init__(self, dataset, model, optimizer, criterion, lr_scheduler, device, lam=0.5):
        super().__init__(dataset, model, optimizer, criterion, lr_scheduler, device)
        self.lam = lam
        self.train_loss_source = []
        self.val_loss_source = []

    def fit(self, train_dataloader_target, train_dataloader_source, val_dataloader_target, val_dataloader_source):
        self.model.train()
        target_loss, source_loss = self.train(train_dataloader_target, train_dataloader_source)
        self.train_losses.append(target_loss)
        self.train_loss_source.append(source_loss)

        val_target_loss, val_source_loss = self.validate(val_dataloader_target, val_dataloader_source)
        self.val_losses.append(val_target_loss)
        self.val_loss_source.append(val_source_loss)

        combined_val_loss = val_target_loss +  val_source_loss

        #print(f'Train Target Loss = {target_loss}, Train Source Loss = {source_loss}, Val Target Loss = {val_target_loss}, Val Source Loss = {val_source_loss}')

        if combined_val_loss < self.best_loss:
            self.best_loss = combined_val_loss
            self.best_model = copy.deepcopy(self.model)
            self.early_stopping_counter = 0
        elif len(self.val_losses) >= 10 and combined_val_loss > min(self.val_losses[-3:] + self.val_loss_source[-3:]):
            self.early_stopping_counter += 1
            if self.early_stopping_counter >= self.patience:
                print("Early stopping")
                self.stopping = True
        else:
            self.early_stopping_counter = 0

        self.lr_scheduler.step()

    def train(self, dataloader_target, dataloader_source):
        return self._process_dataloader(dataloader_target, dataloader_source, validate=False)

    def validate(self, dataloader_target, dataloader_source):
        self.model.eval()
        return self._process_dataloader(dataloader_target, dataloader_source, validate=True)

    def _process_dataloader(self, dataloader_target, dataloader_source, validate=False):
        target_loss = 0
        source_loss = 0
        # Process target dataloader
        for x, y in dataloader_target:
            x, y = to_device(x, self.device), to_device(y, self.device)
            loss = self._step_transfer(x, y, validate, target=True)
            target_loss += loss.item()

        # Process source dataloader
        for x, y in dataloader_source:
            x, y = to_device(x, self.device), to_device(y, self.device)
            loss = self._step_transfer(x, y, validate, target=False)
            source_loss += loss.item()
        return target_loss / len(dataloader_target), source_loss / len(dataloader_source)

    def _step_transfer(self, x, y, validate, target):
        outputs = self.model(x.float())
        loss = self._compute_loss(outputs, y)
        if not target:
            loss = loss * self.lam
        if not validate:
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()
        return loss



class FederatedModelTrainer(ModelTrainer):
    def __init__(self, dataset, model, optimizer, criterion, lr_scheduler, device, dataloader_1, dataloader_2, pfedme = False, pfedme_reg =1e-1):
        super().__init__(dataset, model, optimizer, criterion, lr_scheduler, device)
        self.model_1, self.criterion_1, self.optimizer_1, self.lr_scheduler_1 = self._clone_model()
        self.model_2, self.criterion_2, self.optimizer_2, self.lr_scheduler_2 = self._clone_model()
        total_samples_1, total_samples_2 = len(dataloader_1.dataset), len(dataloader_2.dataset)
        self.weight_1 = total_samples_1 / (total_samples_1 + total_samples_2)
        self.weight_2 = total_samples_2 / (total_samples_1 + total_samples_2)
        self.total_weight = self.weight_1 + self.weight_2
        self.pfedme = pfedme
        self.pfedme_reg = pfedme_reg
        self.save = False

    def _clone_model(self):
        model_clone = copy.deepcopy(self.model)
        criterion_clone = copy.deepcopy(self.criterion)
        optimizer_clone = type(self.optimizer)(model_clone.parameters(), **self.optimizer.defaults)
        lr_scheduler_clone = copy.deepcopy(self.lr_scheduler)
        return model_clone, criterion_clone, optimizer_clone, lr_scheduler_clone

    def fit(self, data_loader_1, data_loader_2, val_data_loader_1, val_data_loader_2):
        self.model_1.train()
        self.model_2.train()
        for i in range(FED_EPOCH):
            if i == FED_EPOCH - 1:
                self.save = True
            else:
                self.save = False
            self._train_site(data_loader_1, 1)
            self._train_site(data_loader_2, 2)
        
        self._fed_avg()

        val_loss = self.validate(val_data_loader_1, val_data_loader_2)
        self.val_losses.append(val_loss)

        #print(f'Train Loss = {self.train_losses[-1]}, Val Loss = {self.val_losses[-1]}')
        if val_loss < self.best_loss:
            self.best_loss = val_loss
            self.best_model = copy.deepcopy(self.model)
            self.early_stopping_counter = 0
        elif len(self.val_losses) >= 10 and val_loss > min(self.val_losses[-3:]):
            self.early_stopping_counter += 1
            if self.early_stopping_counter >= self.patience:
                print("Early stopping")
                self.stopping = True
        else:
            self.early_stopping_counter = 0

        self.lr_scheduler.step()
        self.lr_scheduler_1.step()
        self.lr_scheduler_2.step()

    def _train_site(self, data_loader, site_number):
        model_site, criterion_site, optimizer_site, lr_scheduler_site   = self._get_site_objects(site_number)
        train_loss = 0
        for x, y in data_loader:
            x, y = to_device(x, self.device), to_device(y, self.device)
            loss = self._compute_loss_fed(model_site, criterion_site, x, y)
            train_loss += loss.item()
            optimizer_site.zero_grad()
            loss.backward()
            optimizer_site.step()
        lr_scheduler_site.step() 
        train_loss /= len(data_loader)
        if (site_number == 1) and (self.save):
            self.train_losses.append(train_loss)
   
    def _compute_loss_fed(self, model, criterion, x, y):
        outputs = model(x.float())
        loss_function_fed = self._setup_loss_function_fed(criterion)
        loss = loss_function_fed(outputs, y)
        if self.pfedme:
            regularization_loss = 0
            for p, g_p in zip(model.parameters(), self.model.parameters()):
                regularization_loss += torch.norm(p - g_p)
            regularization_loss = self.pfedme_reg * regularization_loss
            loss += regularization_loss
        return loss

    def _setup_loss_function_fed(self, criterion):
        if self.dataset in DATASET_TYPES_NUMERIC:
            return lambda outputs, y: criterion(outputs.squeeze(), y.float().squeeze())
        elif self.dataset in DATASET_TYPES_CATEGORICAL:
            return lambda outputs, y: criterion(outputs.squeeze(), y.long().squeeze())

    def _fed_avg(self):
        for name, param in self.model.named_parameters():
            weighted_avg_param = (self.weight_1 * self.model_1.state_dict()[name] + self.weight_2 * self.model_2.state_dict()[name]) / self.total_weight
            self.model.state_dict()[name].copy_(weighted_avg_param)
        if not self.pfedme:
            self.model_1.load_state_dict(self.model.state_dict())
            self.model_2.load_state_dict(self.model.state_dict())

    def validate(self, dataloader_1, dataloader_2):
        self.model_1.eval()
        self.model_2.eval()
        val_loss_1, val_loss_2 = 0, 0
        
        for x, y in dataloader_1:
            x, y = to_device(x, self.device), to_device(y, self.device)
            outputs_1 = self.model_1(x.float())
            loss = self._compute_loss(outputs_1, y)
            val_loss_1 += loss.item()
        
        for x, y in dataloader_2:
            x, y = to_device(x, self.device), to_device(y, self.device)
            outputs_2 = self.model_2(x.float())
            loss = self._compute_loss(outputs_2, y)
            val_loss_2 += loss.item()
        
        val_loss_1 /= len(dataloader_1)
        val_loss_2 /= len(dataloader_2)
        combined_val_loss = self.weight_1 * val_loss_1 + self.weight_2 * val_loss_2
        
        return combined_val_loss

    def _get_site_objects(self, site_number):
        return (self.model_1, self.criterion_1, self.optimizer_1, self.lr_scheduler_1) if site_number == 1 else (self.model_2, self.criterion_2, self.optimizer_2, self.lr_scheduler_2)


def get_dice_score(output, target, SPATIAL_DIMENSIONS = (2, 3, 4), epsilon=1e-9):
    p0 = output
    g0 = target
    p1 = 1 - p0
    g1 = 1 - g0
    tp = (p0 * g0).sum(dim=SPATIAL_DIMENSIONS)
    fp = (p0 * g1).sum(dim=SPATIAL_DIMENSIONS)
    fn = (p1 * g0).sum(dim=SPATIAL_DIMENSIONS)
    num = 2 * tp
    denom = 2 * tp + fp + fn + epsilon
    dice_score = num / denom
    return dice_score.mean()

def get_dice_loss(output, target):
    return 1 - get_dice_score(output, target)