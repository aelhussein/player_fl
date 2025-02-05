""" This is a standalone .py file for readability. It is used to track model metrics including:
        - Layer importances
        - Layer activations
        - Model similarities """

# Standard library imports
import copy
import gc
import os
import pickle
import random
import sys
import warnings
from itertools import itertools

# Third-party imports
import numpy as np
import pandas as pd
import scipy.stats
import torch
import torch.nn as nn
import torch.nn.functional as F
from netrep.metrics import LinearMetric
from netrep.conv_layers import convolve_metric
from sklearn.metrics import (
    balanced_accuracy_score,
    f1_score,
    matthews_corrcoef
)
from torch.nn.utils import clip_grad_norm_
from torch.optim.lr_scheduler import ExponentialLR
from torch.utils.data import (
    ConcatDataset,
    DataLoader,
    RandomSampler,
    random_split
)
import argparse

# Local imports
ROOT_DIR = '/gpfs/commons/groups/gursoy_lab/aelhussein/layer_pfl'
sys.path.append(f'{ROOT_DIR}/code')
sys.path.append(f'{ROOT_DIR}/code/helper')

import configs as cs
import dataset_processing as dp

# Configuration
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
cpus = int(os.getenv('SLURM_CPUS_PER_TASK', 6))

# Warning suppressions
warnings.simplefilter(action='ignore', category=FutureWarning)
warnings.simplefilter(action='ignore', category=UserWarning)

# Set random seeds
cs.set_seeds()
def clear_data():
    gc.collect()
    torch.cuda.empty_cache()

ATTENTION_MODELS = cs.ATTENTION_MODELS
CLIP_GRAD = cs.CLIP_GRAD

class ModelDeviceManager:
    def __init__(self, model, device):
        self.model = model
        self.device = device

    def __enter__(self):
        self.model.to(self.device)
        return self.model

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.model.to('cpu')



# Runs per dataset and lr
RUNS =  {'EMNIST':3,
        'CIFAR':1,
        "FMNIST":3,
        "ISIC":1,
        "Sentiment": 3,
        "Heart": 10,
        "mimic":3}

class BaseTrainer:
    """ Base trainer """
    def __init__(self, device, dataset, num_sites, num_classes, layers_to_include = None, epochs = 50, batch_size = 256, cross_device = False):
        self.device = device
        self.dataset = dataset
        self.num_sites = num_sites
        self.TRAIN_SIZE = 0.7
        self.VAL_SIZE = 0.2
        self.BATCH_SIZE = batch_size
        self.EPOCHS = epochs
        self.num_classes = num_classes
        self.cross_device = cross_device

    def create_loaders(self, full_dataset):
        """ Splite data and create loaders """   
        train_num = int(self.TRAIN_SIZE * len(full_dataset.labels))
        val_num = int(self.VAL_SIZE * len(full_dataset.labels))
        test_num = len(full_dataset) - train_num - val_num
        train_dataset, val_dataset, test_dataset = random_split(full_dataset, [train_num, val_num, test_num])
        train_loader = DataLoader(train_dataset, batch_size=self.BATCH_SIZE, shuffle=True, num_workers=2, pin_memory=True)
        val_loader = DataLoader(val_dataset, batch_size=self.BATCH_SIZE, shuffle=False, num_workers=2, pin_memory=True)
        test_loader = DataLoader(test_dataset, batch_size=self.BATCH_SIZE, shuffle=False, num_workers=2, pin_memory=True)
        return train_loader, val_loader, test_loader

    def create_site_objects(self, data, model, loss_fct, LRs):    
        """ Create objects for each site """    
        fed_weights = np.array([len(data[site].labels) for site in data])
        fed_weights = fed_weights / np.sum(fed_weights)
        site_objects = {}
        gamma = 0.99 if self.dataset == 'CIFAR' else 0.9
        if isinstance(LRs, tuple):
            LR_federated, _ = LRs
        elif isinstance(LRs, float):
            LR_federated = LRs
        for site, data_site in data.items():
            #Dataloaders
            site_objects[site] = {}
            site_objects[site]['train_loader'], site_objects[site]['val_loader'], site_objects[site]['test_loader']= self.create_loaders(data_site)
            #model
            site_objects[site]['model'] = copy.deepcopy(model)
            site_objects[site]['best_model'] = copy.deepcopy(model)
            site_objects[site]['best_loss'] = np.inf
            #Trainers
            site_objects[site]['criterion'] = loss_fct
            site_objects[site]['optimizer'] = torch.optim.AdamW(site_objects[site]['model'].parameters(), lr = LR_federated, amsgrad=True, betas=(0.9, 0.999))
            site_objects[site]['lr_scheduler'] = ExponentialLR(site_objects[site]['optimizer'], gamma=gamma)
            site_objects[site]['fed_weight'] = fed_weights[site]
        #global objects
        self.site_objects = site_objects
        self.best_loss = np.inf
        self.global_model = copy.deepcopy(model)
        self.patience = 3 if self.dataset in ['ISIC', 'mimic'] else 10
        self.counter = 0

    def load_site_training_objects(self, site):
        """ Load training objects """        
        model = self.site_objects[site]['model']
        dataloader = self.site_objects[site]['train_loader']
        criterion = self.site_objects[site]['criterion']
        optimizer = self.site_objects[site]['optimizer']
        lr_scheduler = self.site_objects[site]['lr_scheduler']
        return model, dataloader, criterion, optimizer, lr_scheduler
    
    def process_dataloader_items(self, features, labels, device = None):
        """ Put models onto correct devices """
        if device is None:
            device = self.device
        if self.dataset in ATTENTION_MODELS:
            embedding, mask = features
            mask = mask.to(device)
            embedding = embedding.to(device)
            features = (embedding, mask)
        else:
            features = features.to(device)
        labels = labels.to(device)
        return features, labels
    
    def train(self, site):
        """ Train each site separately """
        with ModelDeviceManager(self.site_objects[site]['model'], self.device) as model_on_device:
            model_on_device.train()
            total_loss = 0.0
            for features, labels in self.site_objects[site]['train_loader']:
                features, labels = self.process_dataloader_items(features, labels)
                outputs = model_on_device(features)
                loss = self.site_objects[site]['criterion'](outputs, labels)
                loss.backward()
                if self.dataset in CLIP_GRAD:
                    clip_grad_norm_(model_on_device.parameters(), 5)
                self.site_objects[site]['optimizer'].step()
                self.site_objects[site]['optimizer'].zero_grad(set_to_none=True)
                total_loss += loss.item()
            self.site_objects[site]['lr_scheduler'].step()
        features, labels = self.process_dataloader_items(features, labels, 'cpu')
        torch.cuda.empty_cache()
        return total_loss / len( self.site_objects[site]['train_loader'])

    def concat_datasets(self, site):
        """ Combine the datasets for other sites to measure the generalizability of the model """
        datasets = [self.site_objects[other_site]['test_loader'].dataset for other_site in self.site_objects if site != other_site]
        combined_dataset = ConcatDataset(datasets)
        dataloader = DataLoader(combined_dataset, batch_size=self.site_objects[site]['test_loader'].batch_size, shuffle=False)
        return dataloader


    def evaluate(self, site, test=False):
        """ Evaluate each site """
        if not test:
            model, dataloader, criterion = self.site_objects[site]['model'], self.site_objects[site]['val_loader'], self.site_objects[site]['criterion']
            return self.evaluate_logic(model, dataloader, criterion)
        else:
            model, dataloader, criterion = self.site_objects[site]['best_model'], self.site_objects[site]['test_loader'], self.site_objects[site]['criterion']
            site_loss, site_eval_metrics = self.evaluate_logic(model, dataloader, criterion)
            return [site_loss] + list(site_eval_metrics)
            """ dataloader = self.concat_datasets(site)
            other_loss, other_eval_metrics = self.evaluate_logic(model, dataloader, criterion)
            return [site_loss] + list(site_eval_metrics), [other_loss] + list(other_eval_metrics) """
        

    def evaluate_logic(self, model, dataloader, criterion):
        """ Logic to run evaluation """
        total_loss = 0.0
        all_predicted = []
        true_labels = []
        model.eval()
        with ModelDeviceManager(model, self.device) as model_on_device:
            with torch.no_grad():
                for features, labels in dataloader:
                    features, labels = self.process_dataloader_items(features, labels)
                    outputs = model_on_device(features)
                    loss = criterion(outputs, labels)
                    total_loss += loss.item()
                    _, predicted = torch.max(outputs.data, 1)
                    all_predicted.append(predicted)
                    true_labels.append(labels)
        features, labels = self.process_dataloader_items(features, labels, 'cpu')
        eval_metrics = self.evaluation_metrics(all_predicted, true_labels)
        torch.cuda.empty_cache()
        return total_loss / len(dataloader), eval_metrics
    

    def evaluation_metrics(self, predicted_labels_list, true_labels_list):
        """Compute accuracy, balanced accuracy, F1 score, MCC Score. """
        predicted_labels = torch.cat(predicted_labels_list)
        true_labels = torch.cat(true_labels_list)
        # Accuracy
        correct_predictions = (predicted_labels == true_labels).sum().item()
        accuracy = correct_predictions / len(true_labels)
        
        # Balanced Accuracy
        balanced_accuracy = balanced_accuracy_score(true_labels.cpu().numpy(), predicted_labels.cpu().numpy())
        
        # F1 Score
        f1 = f1_score(true_labels.cpu().numpy(), predicted_labels.cpu().numpy(), average='macro')
        weighted_f1 = f1_score(true_labels.cpu().numpy(), predicted_labels.cpu().numpy(), average='weighted')
        # MCC
        mcc = matthews_corrcoef(true_labels.cpu().numpy(), predicted_labels.cpu().numpy())
        return (accuracy, balanced_accuracy, f1, weighted_f1, mcc)
    
    def initialize_pipeline(self, data, model, loss_fct, LR):
        """ Set up pipeline objects """
        self.create_site_objects(data, model, loss_fct, LR)
        return
    
    def training_loop(self):
        """ Training loop for an epoch """
        train_losses = 0
        for site in self.site_objects:
            train_loss = self.train(site)
            train_losses += train_loss * self.site_objects[site]['fed_weight']
        return train_losses

    def validation_loop(self):
        """ Validation loop for an epoch """
        val_losses = 0
        eval_metrics_list = []
        for site in self.site_objects:
            val_loss, eval_metrics = self.evaluate(site)
            val_losses += val_loss * self.site_objects[site]['fed_weight']
            eval_metrics_list.append(eval_metrics)
            #Save best model for each site
            if val_loss < self.site_objects[site]['best_loss']:
                self.site_objects[site]['best_model'] = copy.deepcopy(self.site_objects[site]['model'])
                self.site_objects[site]['best_loss'] = val_loss
        return val_losses, eval_metrics_list

    def check_early_stopping(self, val_losses):
        """ Early stopping logic """
        if val_losses < self.best_loss:
            self.best_loss = val_losses
            self.best_model = copy.deepcopy(self.global_model)
            self.counter = 0
        else:
            self.counter += 1
            if self.counter > self.patience:
                return True
        return False
    
    def testing_loop(self, eval_metrics_list):
        """ Get performance in test set """
        site_metrics_all = [0 for i in range(len(eval_metrics_list[0]))] + [0] #for test loss
        #other_metrics_all = [0 for i in range(len(eval_metrics_list[0]))] + [0] #for test loss
        site_separate_results = {}
        for site in self.site_objects:
            site_metrics = self.evaluate(site, test=True)
            site_separate_results[site] = site_metrics
            site_metrics_all = [site_metrics_all[i] + site_metrics[i] * self.site_objects[site]['fed_weight'] for i in range(len(site_metrics))]
            #other_metrics_all = [other_metrics_all[i] + other_metrics[i] * self.site_objects[site]['fed_weight'] for i in range(len(other_metrics))]
        #Return the site test loss for printing    
        return site_metrics_all[0], site_metrics_all, site_separate_results

    def clear_pipeline_data(self):
        """ Clear pipeline objects """
        del self.site_objects
        del self.global_model
        clear_data()

    def run_pipeline(self, data, model, loss_fct, LR, clear = True):
        """ Full pipeline """
        self.initialize_pipeline(data, model, loss_fct, LR)
        train_losses_list, val_losses_list = [], []
        #train and validate
        for epoch in range(self.EPOCHS):
            train_losses = self.training_loop()
            val_losses, eval_metrics_list = self.validation_loop()
            train_losses_list.append(train_losses)
            val_losses_list.append(val_losses)
            if epoch % 10 == 0:
                print(f"Epoch {epoch+1}/{self.EPOCHS}, Train Loss: {train_losses:.4f}, Validation Loss: {val_losses:.4f}")
            stop = self.check_early_stopping(val_losses)
            if stop:
                print(f'Early stopping at {epoch}')
                break
        
        test_losses, eval_metrics_all, site_separate_results = self.testing_loop(eval_metrics_list)
        clear_data()
        print(f"Test Loss: {test_losses:.4f}")
        print('\n')
        if clear:
            self.clear_pipeline_data()
        return eval_metrics_all, site_separate_results, [train_losses_list, val_losses_list]



class SingleTrainer(BaseTrainer):
    """ Base single trainer """
    def __init__(self, device, dataset, num_sites, num_classes, layers_to_include = None, epochs = 50, batch_size = 64):
        super().__init__(device, dataset, num_sites, num_classes, layers_to_include,  epochs, batch_size)
    
    def run_pipeline(self, data, model, loss_fct, LR, clear = True):
        """ Full pipeline """
        self.initialize_pipeline(data, model, loss_fct, LR)
        #train and validate
        model_metrics = {'first':{}, 'best':{}}
        model_sim = {'first':{}, 'best':{}}
        train_losses_list = []
        val_losses_list = []
        for epoch in range(self.EPOCHS):
            train_losses = self.training_loop()
            val_losses, eval_metrics_list = self.validation_loop()
            train_losses_list.append(train_losses)
            if epoch == 0:
                for site in self.site_objects:
                    model_metrics['first'][site] = self.get_model_metrics(site)
                model_sim['first'] = self.compare_model_similarity() 
            val_losses_list.append(val_losses)
            if epoch % 1 == 0:
                print(f"Epoch {epoch+1}/{self.EPOCHS}, Train Loss: {train_losses:.4f}, Validation Loss: {val_losses:.4f}")
            stop = self.check_early_stopping(val_losses)
            if stop:
                print(f'Early stopping at {epoch}')
                break
            clear_data()
          
        for site in range(self.num_sites):
            model_metrics['best'][site] = self.get_model_metrics(site, last = True)
        model_sim['best'] = self.compare_model_similarity()
      
        test_losses, eval_metrics_all, site_results = self.testing_loop(eval_metrics_list)
        clear_data()
        print(f"Test Loss: {test_losses:.4f}")
        
        if clear:
            self.clear_pipeline_data()
        return eval_metrics_all, model_metrics, model_sim

    def _random_data_sample(self, site):
        """ Sample from data for model metrics """
        random_sampler = RandomSampler(self.site_objects[site]['train_loader'].dataset, replacement=True)
        random_loader = DataLoader(self.site_objects[site]['train_loader'].dataset, batch_size=self.site_objects[site]['train_loader'].batch_size, sampler=random_sampler)
        return next(iter(random_loader))

    def _prepare_data_multiple_sites(self):
        """ Combine data from all sites for model similarity calculation """
        data_list, label_list = [], []
        for site in self.site_objects:
            batch_data, batch_label = self._random_data_sample(site)
            samples = len(batch_label) // self.num_sites
            data_list.append(batch_data[:samples])
            label_list.append(batch_label[:samples])

        labels = torch.cat(label_list)
        if isinstance(data_list[0], (list, tuple)):
            masks, features = zip(*data_list)
            data = (torch.cat(features), torch.cat(masks))
        else:
            data = torch.cat(data_list)
        return data, labels
    
    def _model_pass(self, model, data, label):
        """ One pass through model """        
        criterion = nn.CrossEntropyLoss()
        #data, label  = self.process_dataloader_items(data,label)
        #model = model.to(self.device)
        outputs = model(data)
        model.zero_grad()
        self.loss_analysis = criterion(outputs, label)
        self.loss_analysis.backward(retain_graph=True)
        data, label  = self.process_dataloader_items(data,label, 'cpu')
        return model

    def _process_layer(self, param, grad, data = None, emb_table = False):
        """ Process the activation and weight values for a layer. """
        layer_data = model_weight_data(param)
        layer_data.update(model_weight_importance(param, grad, data, emb_table))
        return layer_data

    def _analyse_weights_grad(self, model, data):
        """ Extract params and gradients per layer and channel """
        layer_data_dict = {}
        named_params = dict(model.named_parameters())
        for name in named_params:
            layer_name = name.replace(".0.weight", "")
            param = named_params[name]
            emb_table = True if any(substring in name for substring in ['token_embedding_table']) else False
            if any(substring in name for substring in ["flatten", "bias"]):
                continue
            if param is None:
                continue
            grad = param.grad
            if any(substring in name for substring in ['fc', 'attention', 'proj', 'resid', 'embedding_table']):
                if any(substring in name for substring in ['fc2', 'position_embedding_table1']):
                    data = None
                hessian_metrics_layer = hessian_metrics(param, self.loss_analysis,  data, emb_table)
                param = param.to('cpu')
                grad = grad.to('cpu')
                layer_data = self._process_layer(param, grad, data,  emb_table)
                layer_data_dict[layer_name] = layer_data
                layer_data_dict[layer_name].update(hessian_metrics_layer)
            elif param.dim() == 4:  # convolutional layer
                out_channels = param.size(0)
                hessian_metrics_all_channels = hessian_metrics(param, self.loss_analysis)
                param = param.to('cpu')
                grad = grad.to('cpu')
                for channel in range(out_channels):
                    param_channel = param[channel]
                    grad_channel = grad[channel]
                    name_channel = f'{layer_name}_c_{channel}'
                    channel_data = self._process_layer(param_channel, grad_channel, data= None, emb_table = False)
                    layer_data_dict[name_channel] = channel_data
                    layer_data_dict[name_channel].update(hessian_metrics_all_channels)
                del hessian_metrics_all_channels
        
        del model, named_params, param, grad, self.loss_analysis
        clear_data()
        return layer_data_dict


    def get_model_metrics(self, site, last=False):
        """ Extract model layer importance and activation data. Layer importance estiamted using first-order gradient estimation """
        model = self.site_objects[site]['best_model'] if last else self.site_objects[site]['model']
        data, label = self._random_data_sample(site)
        model = self._model_pass(model, data, label)
        layer_data_dict = self._analyse_weights_grad(model, data if isinstance(data, (list, tuple)) else None)
        del data, label
        return pd.DataFrame.from_dict(layer_data_dict, orient='index')

    
    def compare_model_similarity(self):
        """ Take models and compare feature representations at each layer using linear Procustes analysis from netrep, Generalized Shape Metrics on Neural Representations """
        models  = [self.site_objects[site]['best_model'] for site in self.site_objects]
        data, labels =self._prepare_data_multiple_sites()
        activations_with_names = {site: [] for site in self.site_objects}
        for site, act_model in enumerate(models):
            register_hooks(act_model, activations_with_names[site])
            data, labels = self.process_dataloader_items(data, labels)
            act_model.eval()
            with torch.no_grad():
                with ModelDeviceManager(act_model, self.device) as model_on_device:
                    _ = model_on_device(data)
                torch.cuda.empty_cache()
            data, labels = self.process_dataloader_items(data, labels, 'cpu')
        
        if isinstance(data, (list, tuple)):
            comparison_data = self._compare_activations(activations_with_names, data)
        else:
            comparison_data = self._compare_activations(activations_with_names)
        del data, activations_with_names
        clear_data()
        return comparison_data

    def _compare_activations(self, activations, data = None):
        """ compare activation maps from models with same data passed through """
        comparison_data = {}
        adjust = 0
        for layer, (name, activation) in enumerate(activations[0][:-1]):
            if "flatten" in name:
                pass
            else:
                print(f'reached {name}')
                conv_layer = True if len(activation.shape) == 4 else False
                att_layer = True if (len(activation.shape) == 3 or 'embedding' in name) else False
                alpha = 1
                metric = LinearMetric(alpha=alpha, center_columns=True, score_method="angular",)
                if conv_layer:
                    layer_act= [np.transpose(activations[site][layer][1],(0,2,3,1)) for site in activations]
                    result = np.zeros((self.num_sites, self.num_sites))
                    for i, j in itertools.combinations(range(self.num_sites), 2):
                        dist = convolve_metric(metric, layer_act[i],layer_act[j] )
                        result[i,j] = dist.min()
                        result[j,i] = dist.min()
                    comparison_data[name] = pd.DataFrame(result, index = [f'site_{i}' for i in range(self.num_sites)], columns = [f'site_{i}' for i in range(self.num_sites)])
                    adjust = 0.5
                elif att_layer:
                    _ , mask = data
                    layer_act= [activations[site][layer][1]for site in activations]
                    mask = mask.bool().unsqueeze(-1).numpy()
                    layer_act = [act * mask for act in layer_act]
                    layer_act = [np.sum(masked_act, axis=1) for masked_act in layer_act]
                    _, result = metric.pairwise_distances(layer_act,layer_act, processes = cpus)
                    comparison_data[name] = pd.DataFrame(result, index = [f'site_{i}' for i in range(self.num_sites)], columns = [f'site_{i}' for i in range(self.num_sites)])
                else:
                    layer_act= [activations[site][layer][1] for site in activations]
                    _, result = metric.pairwise_distances(layer_act,layer_act, processes = cpus)
                    comparison_data[name] = pd.DataFrame(result, index = [f'site_{i}' for i in range(self.num_sites)], columns = [f'site_{i}' for i in range(self.num_sites)]) + adjust
            for site in activations:
                activations[site][layer] = None
            del layer_act
            gc.collect()
        return comparison_data
    
    def _compare_model_weights(self, model_weights):
        """Compare model weights across different sites."""
        comparison_data = {}
        for layer, (name, weights) in enumerate(model_weights[0]):
            print(f'Comparing weights for {name}')
            conv_layer = True if len(weights.shape) == 4 else False
            fc_layer = True if len(weights.shape) == 2 else False

            metric = LinearMetric(alpha=1, center_columns=True, score_method="angular")
            layer_weights = [model_weights[site][layer][1] for site in model_weights]

            if conv_layer:
                # For convolutional layers, transpose to (batch_size, height, width, channels)
                layer_weights = [np.transpose(weights, (2, 3, 0, 1)) for weights in layer_weights]

            if conv_layer or fc_layer:
                result = np.zeros((self.num_sites, self.num_sites))
                for i, j in itertools.combinations(range(self.num_sites), 2):
                    dist = convolve_metric(metric, layer_weights[i], layer_weights[j])
                    result[i, j] = dist.min()
                    result[j, i] = dist.min()
                comparison_data[name] = pd.DataFrame(result, index=[f'site_{i}' for i in range(self.num_sites)], 
                                                    columns=[f'site_{i}' for i in range(self.num_sites)])

            # Clear memory
            for site in model_weights:
                model_weights[site][layer] = None
            del layer_weights
            gc.collect()

        return comparison_data


class FederatedTrainer(SingleTrainer):
    """ Base single trainer """
    def __init__(self, device, dataset, num_sites, num_classes, layers_to_include = None, epochs = 50, batch_size = 64):
        super().__init__(device, dataset, num_sites, num_classes, layers_to_include,  epochs, batch_size)
    
    def run_pipeline(self, data, model, loss_fct, LR, clear = True):
        """ Full pipeline """
        self.initialize_pipeline(data, model, loss_fct, LR)
        #train and validate
        model_metrics = {'first':{}, 'best':{}}
        model_sim = {'first':{}, 'best':{}}
        train_losses_list = []
        val_losses_list = []
        for epoch in range(self.EPOCHS):
            train_losses = self.training_loop()
            self.fed_avg()
            val_losses, eval_metrics_list = self.validation_loop()
            train_losses_list.append(train_losses)
            if epoch == 0:
                for site in self.site_objects:
                    model_metrics['first'][site] = self.get_model_metrics(site)
            val_losses_list.append(val_losses)
            if epoch % 1 == 0:
                print(f"Epoch {epoch+1}/{self.EPOCHS}, Train Loss: {train_losses:.4f}, Validation Loss: {val_losses:.4f}")
            stop = self.check_early_stopping(val_losses)
            if stop:
                print(f'Early stopping at {epoch}')
                break
            clear_data()
          
        for site in range(self.num_sites):
            model_metrics['best'][site] = self.get_model_metrics(site, last = True)
      
        test_losses, eval_metrics_all, site_results = self.testing_loop(eval_metrics_list)
        clear_data()
        print(f"Test Loss: {test_losses:.4f}")
        
        if clear:
            self.clear_pipeline_data()
        return eval_metrics_all, model_metrics, model_sim


    def fed_avg(self):
        """ Fed avg based on sample sizes """
        models = [(site,self.site_objects[site]['model']) for site in self.site_objects]
        fed_weights = [self.site_objects[site]['fed_weight'] for site in self.site_objects]
        
        for param in self.global_model.parameters():
            param.data *= 0
        
        for i, (site,model) in enumerate(models):
            with ModelDeviceManager(model, self.device) as model_on_device , ModelDeviceManager(self.global_model, self.device) as global_model:
                fed_weight = fed_weights[i] / sum(fed_weights)
                for m1, m2 in zip(global_model.named_parameters(), model_on_device.named_parameters()):
                    _, avg_param = m1
                    _, model_param = m2
                    avg_param.data += fed_weight * model_param.data
                
        for site in self.site_objects:
            with ModelDeviceManager(self.site_objects[site]['model'], self.device) as model_on_device , ModelDeviceManager(self.global_model, self.device) as global_model:
                model_on_device.load_state_dict(global_model.state_dict())
        return
    
def hook_fn(layer_name, activations_container):
    """ extract the activation map for a layer """
    def hook(module, input, output):
        activations_container.append((layer_name, output.detach().to('cpu').numpy()))
    return hook

def register_hooks(model, activations_container):
    """ extract activation maps for all layers in models """
    for name, module in model.named_modules():
        if '.' not in name: 
            module.register_forward_hook(hook_fn(name, activations_container))

def model_weight_data(param):
    """ Get data on the weights per layer imcluding mean, variance and % above certain magnitude"""
    #store weight details
    data = {
        'Weight Mean': abs(param).mean().item(),
        'Weight Variance': param.var().item(),
    }
    for threshold in [0.01, 0.05, 0.1]:
        small_weights = (param.abs() < threshold).sum().item()
        data[f'% Weights < {threshold}'] = 100 * small_weights / param.numel()
    return data


def model_activation_data(name, activation, data = None):
    """ Get the activation metrics for each layer """
    """ if name == 'position_embedding_table1':
            return {
        'Activation Mean': None,
        '% Non-zero Activations': None,
        'Activation variance: Layer': None,
        'Activation variance: Samples': None
    } """
    if data is None:
        #% non-zero
        mean_across_layer = activation.mean().item()
        total_elements = activation.numel()
        non_zero_elements =  (activation != 0).sum().item()
        percentage_non_zero = 100.0 * non_zero_elements / total_elements
        # Variance across the layer (variance in the layer per sample then take the mean)
        variance_across_layer = activation.var(dim=1).mean().item()
        #Variance across samples (variance in 1 neurone across samples then take then means)
        variance_across_samples = activation.var(dim=0).mean().item()
    else:
        token_indices, mask = data
        expanded_mask = mask.unsqueeze(-1).expand_as(activation)
        masked_activation = activation * expanded_mask
        non_zero_elements = (masked_activation != 0).sum().item()
        total_unmasked_elements = expanded_mask.sum().item()
        percentage_non_zero = 100.0 * non_zero_elements / total_unmasked_elements if total_unmasked_elements > 0 else 0
        mean_across_layer = abs((masked_activation.sum(dim = (1,2)) / expanded_mask.sum(dim = (1,2))).unsqueeze(1).unsqueeze(2)).mean().item()
        variance_across_layer = ((((masked_activation  - mean_across_layer)**2)*expanded_mask).sum(dim = (1,2)) / expanded_mask.sum(dim=(1,2))).mean().item()
        mean_across_samples = masked_activation.sum(dim=0) / expanded_mask.sum(dim=0)
        mean_across_samples = torch.where(torch.isnan(mean_across_samples), torch.zeros_like(mean_across_samples), mean_across_samples) # replace masked division by 0
        variance_across_samples = ((masked_activation - mean_across_samples)**2 * expanded_mask).sum(dim=0)
        non_nan_mask = ~torch.isnan(variance_across_samples)
        non_zero_mask = variance_across_samples != 0
        combined_mask = non_nan_mask & non_zero_mask
        non_nan_values = variance_across_samples[combined_mask]
        variance_across_samples = non_nan_values.mean().item()
    return {
        'Activation Mean': abs(mean_across_layer),
        '% Non-zero Activations': percentage_non_zero,
        'Activation variance: Layer': variance_across_layer,
        'Activation variance: Samples': variance_across_samples
    }

def model_weight_importance(param, gradient, data = None, emb_table = False):
    """ Estimate model layer importance using first order gradient approx """
    if emb_table:
        token_indices, mask = data
        param = param[token_indices].mean(dim = 0)
        gradient = gradient[token_indices].mean(dim=0)
    importance = (param * gradient).pow(2).sum().item()
    importance_per = importance / param.numel()
    grad_var = gradient.var().item()
    return {
        'Gradient Importance': importance,
        'Gradient Importance per': importance_per,
        'Gradient Variance': grad_var
    }

def compute_hessian(param, loss):
    """Compute Hessian matrix for a given parameter and loss."""
    # Ensure the gradient of the loss with respect to the parameter is computed
    first_grad = torch.autograd.grad(loss, param, create_graph=True)[0]
    generator = torch.Generator()
    generator.manual_seed(seed)
    dummy_param = torch.randn(param.size(), generator = generator) 
    dummy_param /= torch.norm(dummy_param)
    hessian = torch.autograd.grad(first_grad, param, grad_outputs=dummy_param, create_graph=True)[0]
    return hessian

def compute_eigenvalues(hessian):
    """ Get the eigenvalues """
    #svd
    eigenvalues = torch.linalg.svdvals(hessian)
    sum_eigenevalues = torch.sum(eigenvalues)

    #gram matrix
    gram = hessian.T @ hessian
    gram_eigenvalues = torch.linalg.eigvals(gram).real
    gram_sum_eigenevalues = torch.sum(gram_eigenvalues)
    
    return eigenvalues, sum_eigenevalues.item(), gram_eigenvalues.real, gram_sum_eigenevalues.real.item()

def hessian_metrics(param, loss, data = None, emb_table = False):
    """ Calculate the Hessian of a layer and extract eigenvalues."""
    hessian = compute_hessian(param, loss)
    hessian = hessian.detach().to('cpu')
    param = param.detach().to('cpu')
    if emb_table:
        token_indices, mask = data
        param = param[token_indices].mean(dim = 0)
        hessian = hessian[token_indices].mean(dim = 0)
    importance = (param * hessian).pow(2).sum().item()
    importance_per = importance / param.numel()
    hessian_var = hessian.var().item()
    if len(hessian.shape) == 2:
        svd_e, svd_sum_e, gram_e, gram_sum_e = compute_eigenvalues(hessian)
    elif emb_table:
        svd_e, svd_sum_e, gram_e, gram_sum_e = [] , 0, [], 0
        batch_size = token_indices.shape[0]
        for sample in range(batch_size):
            hessian_s = hessian[sample].squeeze(0)
            svd_e_s, svd_sum_e_s, gram_e_s, gram_sum_e_s  = compute_eigenvalues(hessian_s)
            svd_e.append(svd_e_s)
            svd_sum_e += svd_sum_e_s / batch_size
            gram_e.append(gram_e_s)
            gram_sum_e += gram_sum_e_s / batch_size
        svd_e = (torch.cat(svd_e) if svd_e else torch.tensor([])) / batch_size
        gram_e = (torch.cat(gram_e) if gram_e else torch.tensor([])) / batch_size

    elif len(hessian.shape) == 4:
        c_out, c_in, _, _ = hessian.shape 
        svd_e, svd_sum_e, gram_e, gram_sum_e = [] , 0, [], 0
        for channel in range(c_in):
            channel_hessian = hessian[:,channel].reshape(c_out, -1)
            svd_e_c, svd_sum_e_c, gram_e_c, gram_sum_e_c= compute_eigenvalues(channel_hessian) #Hessian for the input channels
            svd_e.append(svd_e_c)
            svd_sum_e += svd_sum_e_c / c_in
            gram_e.append(gram_e_c)
            gram_sum_e += gram_sum_e_c / c_in
        svd_e = (torch.cat(svd_e) if svd_e else torch.tensor([])) / c_in
        gram_e = (torch.cat(gram_e) if gram_e else torch.tensor([])) / c_out
    
    svd_e_np = svd_e.numpy()
    skewness = scipy.stats.skew(svd_e_np)
    threshold = 0.01 * max(svd_e_np)
    proportion_small = 100 * np.sum(svd_e_np < threshold) / len(svd_e_np)
    proportion_neg = 100 * np.sum(svd_e_np < 0) / len(svd_e_np)
    condition_number = svd_e_np.max() / (np.min([svd_e_np > 0]) + 1e-12)
    operator_norm = max(svd_e_np)

    return {
        'SVD Eigenvalues': svd_e,
        'SVD Sum EV': svd_sum_e,
        'Gram Eigenvalues': gram_e,
        'Gram Sum EV': gram_sum_e,
        'EV Skewness': skewness,
        f'% EV small': proportion_small,
        f'% EV neg': proportion_neg,
        'Gradient Importance 2': importance,
        'Gradient Importance per 2': importance_per,
        'Hessian Variance': hessian_var,
        'Condition Number': condition_number,
        'Operator norm': operator_norm
    }


def l2_norm_difference(tensor1, tensor2):
    """ Get the L2 norm between 2 layer weights """
    diff = tensor1 - tensor2
    return torch.norm(diff, 2).item()

def cosine_sim(tensor1, tensor2):
    """ Get the cosine similarity between 2 layer weights """
    cos_sim= 1 - F.cosine_similarity(tensor1, tensor2).mean().item()
    return cos_sim

def correlation(tensor1, tensor2):
    """ Get the cosine similarity between 2 layer weights """
    tensor1_flat = tensor1.view(-1).unsqueeze(0)
    tensor2_flat = tensor2.view(-1).unsqueeze(0)
    weights = torch.concat([tensor1_flat,tensor2_flat])
    correlation_coef = torch.corrcoef(weights)[0,1].item()
    return correlation_coef

def model_rep_similarity(tensor1, tensor2):
    """ Calculate the similarity using linear centered kernel alignment """
    #combine across channels
    if tensor1.dim() > 2:
        tensor1 = tensor1.sum(dim=tuple(range(2, tensor1.dim())), keepdim=True).squeeze()
    if tensor2.dim() > 2:
        tensor2 = tensor2.sum(dim=tuple(range(2, tensor2.dim())), keepdim=True).squeeze()
    #calc cka per sample
    tensor1 = tensor1 - tensor1.mean(0)
    tensor2 = tensor2 - tensor2.mean(0)
    numerator = torch.norm(torch.matmul(tensor2.T, tensor1), p='fro') ** 2
    denominator = torch.norm(torch.matmul(tensor1.T, tensor1), p='fro') * torch.norm(torch.matmul(tensor2.T, tensor2), p='fro')
    return (numerator / denominator).item()


def state_dicts_are_close(state_dict1, state_dict2, atol=1e-8):
    """Check if two state_dicts are close."""
    for key in state_dict1:
        if key not in state_dict2:
            return False
        if not torch.allclose(state_dict1[key], state_dict2[key], atol=atol):
            return False
    return True

def save_data(dataset, results, model_metrics, model_sim, federated):
    if federated:
        save_dict = {'results':results, "metrics":model_metrics}
        with open(f'{ROOT_DIR}/results/{dataset}_layer_analysis_fed.pkl', 'wb') as f:
            pickle.dump(save_dict, f)
    else:
        save_dict = {'results':results, "metrics":model_metrics,"similarity":model_sim}
        with open(f'{ROOT_DIR}/results/{dataset}_layer_analysis.pkl', 'wb') as f:
            pickle.dump(save_dict, f)

def run_model_metric_pipeline(dataset, dataloader, natural_partition, federated):
    """ Run the model metric pipeline on the single site trainer """
    #load data
    SIZE_PER_SITE = cs.SIZES_PER_SITE_DICT[dataset]
    NUM_SITES = cs.NUM_SITES_DICT[dataset]
    if natural_partition:
        #for data collected at different sites already
        sites_data = [i for i in range(NUM_SITES)]
    else:
        #split the data using dirichlet distribution of labels
        sites_data = dp.create_datasets_per_site(dataloader, SIZE_PER_SITE, NUM_SITES, cs.DATASET_ALPHA[dataset])
    sites_loaders = {}
    for site, site_data in enumerate(sites_data):
        sites_loaders[site] = cs.DATALOADER_DICT[dataset](site_data)
    
    results = {}
    model_metrics = {}
    model_sim = {}
    for run in range(1,RUNS[dataset]+1):
        global seed
        seed = copy.deepcopy(run)
        cs.set_seeds(seed)
        #Run training
        CLASSES = cs.CLASSES_DICT[dataset]
        model = cs.MODEL_DICT[dataset](CLASSES)
        LRs = cs.LR_DICT[dataset]
        epochs = cs.EPOCHS_DICT[dataset]
        loss_fct = cs.LOSSES_DICT[dataset]
        if dataset in cs.FOCAL_LOSS_DATASETS:
            loss_fct = loss_fct(CLASSES, alpha = cs.LOSS_ALPHA[dataset], gamma = cs.LOSS_GAMMA[dataset]) # instantiate with correct number of classes and alpha
        if not federated:
            trainer = SingleTrainer(device, dataset, NUM_SITES, CLASSES, None, epochs, batch_size = 64)
        else:
             trainer = FederatedTrainer(device, dataset, NUM_SITES, CLASSES, None, epochs, batch_size = 64)
        results[run], model_metrics[run], model_sim[run] = trainer.run_pipeline(sites_loaders, model, loss_fct, LRs, clear = True)
        del trainer
        clear_data()
    save_data(dataset, results, model_metrics, model_sim, federated)
    print(f'--------------------{dataset} Completed--------------------')
    return results, model_metrics, model_sim


def main(datasets, federated):

    for dataset in datasets:
        dataloader = cs.DATA_DICT[dataset]
        natural_partition = cs.PARTITION_DICT[dataset]
        run_model_metric_pipeline(dataset, dataloader, natural_partition, federated)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-ds", "--datasets")
    parser.add_argument("-fd", "--federated", type=lambda x: x.lower() == 'true', 
                       default=False, help="Set to 'true' or 'false'")
    args = parser.parse_args()
    
    datasets = args.datasets
    federated = args.federated
    datasets = datasets.split(',')
    
    main(datasets, federated)
