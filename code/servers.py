from configs import *
from helper import *
from clients import *
from models import HyperNetwork

    
class Server:
    """Base server class for federated learning."""
    def __init__(self, config: TrainerConfig, globalmodelstate: ModelState):
        self.config = config
        self.device = config.device
        self.personal =config.requires_personal_model
        self.clients = {}
        self.serverstate = globalmodelstate
        self.serverstate.model = self.serverstate.model.to(self.device)
        self.serverstate.best_model = self.serverstate.best_model.to(self.device)
        
 
    def set_server_type(self, name, tuning):
        self.server_type = name
        self.tuning = tuning
    
    def _create_client(self, clientdata, modelstate, personal_model):
        """Create a client instance."""
        return Client(
            config=self.config,  
            data=clientdata, 
            modelstate=modelstate.copy(),
            metrics_calculator=MetricsCalculator(self.config.dataset_name),
            personal_model=personal_model
        )

    def add_client(self, clientdata: SiteData):
        """Add a client to the federation."""        
        client = self._create_client(
            clientdata=clientdata,
            modelstate=self.serverstate,
            personal_model=self.personal
        )
        
        # Add client to federation
        self.clients[clientdata.site_id] = client
        self._update_client_weights()

    def _update_client_weights(self):
        """Update client weights based on dataset sizes."""
        total_samples = sum(client.data.num_samples for client in self.clients.values())
        for client in self.clients.values():
            client.data.weight = client.data.num_samples / total_samples

    def _aggregate_scores(self, score_dict, client_metrics, weight):
        """Aggregate client score into score dictionary with weight."""
        for metric_name, value in client_metrics.items():
            if metric_name not in score_dict:
                score_dict[metric_name] = 0.0
            score_dict[metric_name] += value * weight
        return score_dict

    def train_round(self):
        """Run one round of training."""
        # Train all clients
        train_loss = 0
        val_loss = 0
        val_score = {}
        for client in self.clients.values():
            # Train and validate
            client_train_loss = client.train(self.personal)
            client_val_loss, client_val_score = client.validate(self.personal)
            
            # Weight metrics by client dataset size
            train_loss += client_train_loss * client.data.weight
            val_loss += client_val_loss * client.data.weight
            val_score = self._aggregate_scores(val_score, client_val_score, client.data.weight)
        # Track metrics
        self.serverstate.train_losses.append(train_loss)
        self.serverstate.val_losses.append(val_loss)
        self.serverstate.val_scores.append(val_score)
        # Aggregate and distribute
        self.aggregate_models()
        self.distribute_global_model()

        # Update best model if improved
        if val_loss < self.serverstate.best_loss:
            self.serverstate.best_loss = val_loss
            self.serverstate.best_model = copy.deepcopy(self.serverstate.model)
        return train_loss, val_loss, val_score

    def test_global(self):
        """Test the model across all clients."""
        test_loss = 0
        test_score = {}
        
        for client in self.clients.values():
            client_loss, client_score = client.test(self.personal)
            test_loss += client_loss * client.data.weight
            test_score = self._aggregate_scores(test_score, client_score, client.data.weight)

        self.serverstate.test_losses.append(test_loss)
        self.serverstate.test_scores.append(test_score)

        return test_loss, test_score
    
    def aggregate_models(self):
        """Base aggregation method - to be implemented by subclasses."""
        return
    
    def distribute_global_model(self):
        """Base distribution method - to be implemented by subclasses"""
        return
    
class FLServer(Server):
    """Base federated learning server with FedAvg implementation."""
    def aggregate_models(self):
        """Standard FedAvg aggregation."""
        # Reset global model parameters
        for param in self.serverstate.model.parameters():
            param.data.zero_()
            
        # Aggregate parameters
        for client in self.clients.values():
            client_model = client.personal_state.model if self.personal else client.global_state.model
            for g_param, c_param in zip(self.serverstate.model.parameters(), client_model.parameters()):
                g_param.data.add_(c_param.data * client.data.weight)

    def distribute_global_model(self):
        """Distribute global model to all clients."""
        global_state = self.serverstate.model.state_dict()
        for client in self.clients.values():
            client.set_model_state(global_state)

class FedAvgServer(FLServer):
    """FedAvg server implementation."""
    pass

class FedProxServer(FLServer):
    """FedProx server implementation."""
    def _create_client(self, clientdata, modelstate, personal_model = False):
        """Create a client instance."""
        return FedProxClient(
            config=self.config,  
            data=clientdata, 
            modelstate=modelstate.copy(),  
            metrics_calculator=MetricsCalculator(self.config.dataset_name),
            personal_model=personal_model
        )

class PFedMeServer(FLServer):
    """PFedMe server implementation."""
    def _create_client(self, clientdata, modelstate, personal_model = True):
        """Create a client instance."""
        return PFedMeClient(
            config=self.config,  
            data=clientdata, 
            modelstate=modelstate.copy(),  
            metrics_calculator=MetricsCalculator(self.config.dataset_name),
            personal_model=personal_model
        )

class DittoServer(FLServer):
    """Ditto server implementation."""
    def _create_client(self, clientdata, modelstate, personal_model = True):
        """Create a client instance."""
        return DittoClient(
            config=self.config,  
            data=clientdata, 
            modelstate=modelstate.copy(),  
            metrics_calculator=MetricsCalculator(self.config.dataset_name),
            personal_model=personal_model
        )
    
    def train_round(self):
        """Run one round of training."""
        # First do global model updates (FedAvg style)
        global_train_loss = 0
        global_val_loss = 0
        global_val_score = {}

        # 1. Global model update step (like FedAvg)
        for client in self.clients.values():
            # Train and validate global model
            client_train_loss = client.train(personal=False)  # Force global update
            client_val_loss, client_val_score = client.validate(personal=False)
            
            # Weight metrics by client dataset size
            global_train_loss += client_train_loss * client.data.weight
            global_val_loss += client_val_loss * client.data.weight
            global_val_score = self._aggregate_scores(global_val_score, client_val_score, client.data.weight)

        # Aggregate and distribute global model
        self.aggregate_models()
        self.distribute_global_model()

        # 2. Personal model update step (Ditto)
        personal_train_loss = 0
        personal_val_loss = 0
        personal_val_score = {}

        for client in self.clients.values():
            # Train and validate personal model
            client_train_loss = client.train(personal=True)  # Personal update
            client_val_loss, client_val_score = client.validate(personal=True)
            
            # Weight metrics by client dataset size
            personal_train_loss += client_train_loss * client.data.weight
            personal_val_loss += client_val_loss * client.data.weight
            personal_val_score = self._aggregate_scores(personal_val_score, client_val_score, client.data.weight)

        self.serverstate.train_losses.append(personal_train_loss)
        self.serverstate.val_losses.append(personal_val_loss)
        self.serverstate.val_scores.append(personal_val_score)

        # Update best model if improved (using global model performance)
        if global_val_loss < self.serverstate.best_loss:
            self.serverstate.best_loss = global_val_loss
            self.serverstate.best_model = copy.deepcopy(self.serverstate.model)

        return personal_train_loss, personal_val_loss, personal_val_score

class LocalAdaptationServer(FLServer):
    """Local adaptation server implementation."""
    def _create_client(self, clientdata, modelstate, personal_model = False):
        """Create a client instance."""
        return LocalAdaptationClient(
            config=self.config,  
            data=clientdata, 
            modelstate=modelstate.copy(),  
            metrics_calculator=MetricsCalculator(self.config.dataset_name),
            personal_model=personal_model
        )
    
    def train_round(self, final_round = False):
        """Run one round of training with optional final round behavior."""
        # Use parent class training logic
        train_loss, val_loss, val_score = super().train_round()

        if final_round:
            train_loss = 0
            val_loss = 0
            val_score = {}
            for client in self.clients.values():
                client_train_loss = client.train(self.personal, final_round = True)
                client_val_loss, client_val_score = client.validate(self.personal)
            
            # Weight metrics by client dataset size
            train_loss += client_train_loss * client.data.weight
            val_loss += client_val_loss * client.data.weight
            val_score = self._aggregate_scores(val_score, client_val_score, client.data.weight)


        return train_loss, val_loss, val_score
    
class LayerServer(FLServer):
    """Server for layer-wise federated learning."""
    def aggregate_models(self):
        """Aggregate only specified layers."""
        layers_to_include = self.config.algorithm_params['layers_to_include']
        
        # Reset parameters of federated layers
        for name, param in self.serverstate.model.named_parameters():
            if any(layer in name for layer in layers_to_include):
                param.data.zero_()
        
        # Aggregate only federated layers
        for client in self.clients.values():
            client_state = client.get_client_state(personal = False)
            client_model = client_state.model.state_dict()
            for name, param in self.serverstate.model.named_parameters():
                if any(layer in name for layer in layers_to_include):
                    param.data.add_(client_model[name].data * client.data.weight)
                    
    def distribute_global_model(self):
        """Distribute only specified layers of global model to clients."""
        layers_to_include = self.config.algorithm_params['layers_to_include']
        
        # Get current global state
        global_state = self.serverstate.model.state_dict()
        
        for client in self.clients.values():
            # Get current client state
            current_client_state = client.get_client_state(personal=False).model.state_dict()
            
            # Create new state dict combining global and client states
            new_state = current_client_state.copy()  # Start with current client state
            
            # Update only specified layers from global model
            for name in new_state.keys():
                if any(layer in name for layer in layers_to_include):
                    new_state[name] = global_state[name].clone()
                    
            # Set complete state dict back to client
            client.set_model_state(new_state)

class LayerPFLServer(LayerServer):
    """Server for layer-wise federated learning."""
    def _create_client(self, clientdata, modelstate, personal_model = False):
        """Create a client instance."""
        return LayerPFLClient(
            config=self.config,  
            data=clientdata, 
            modelstate=modelstate.copy(),  
            metrics_calculator=MetricsCalculator(self.config.dataset_name),
            personal_model=personal_model
        )
class BABUServer(LayerServer):
    """Server implementation for BABU."""
    def _create_client(self, clientdata, modelstate, personal_model = False):
        """Create a client instance."""
        return BABUClient(
            config=self.config,  
            data=clientdata, 
            modelstate=modelstate.copy(),  
            metrics_calculator=MetricsCalculator(self.config.dataset_name),
            personal_model=personal_model
        )
    
    def train_round(self, final_round = False):
        """Run one round of training with final round head tuning."""
        # Use parent class training logic
        train_loss, val_loss, val_score = super().train_round()

        if final_round:
            #Train the head
            train_loss = 0
            val_loss = 0
            val_score = {}
            for client in self.clients.values():
                client_train_loss = client.train_head() 
                client_val_loss, client_val_score = client.validate(self.personal)
        
            train_loss += client_train_loss * client.data.weight
            val_loss += client_val_loss * client.data.weight
            val_score = self._aggregate_scores(val_score, client_val_score, client.data.weight)


        return train_loss, val_loss, val_score
    

class FedLPServer(FLServer):
    """Server implementation for FedLP."""
    def _create_client(self, clientdata, modelstate, personal_model = False):
        """Create a client instance."""
        return FedLPClient(
            config=self.config,  
            data=clientdata, 
            modelstate=modelstate.copy(),  
            metrics_calculator=MetricsCalculator(self.config.dataset_name),
            personal_model=personal_model
        )
    
    def aggregate_models(self):
        """Aggregate pruned models layer-wise, keeping original params if no participation."""
        # Track participating clients for each layer
        layer_participants = {}  
        
        # Collect pruned models and their indicators
        client_models = {}
        client_indicators = {}
        for client_id, client in self.clients.items():
            pruned_state, indicators = client.get_pruned_model_state()
            client_models[client_id] = pruned_state
            client_indicators[client_id] = indicators
            
            # Track which clients participate in each layer
            for layer_name, indicator in indicators.items():
                if indicator:
                    if layer_name not in layer_participants:
                        layer_participants[layer_name] = []
                    layer_participants[layer_name].append(client_id)

        # Create new state dict for aggregated model
        new_state_dict = self.serverstate.model.state_dict()
        
        # Layer-wise aggregation
        for name, param in new_state_dict.items():
            layer_name = name.split('.')[0]
            if layer_name in layer_participants and layer_participants[layer_name]:
                # Layer has participants - aggregate their parameters
                participants = layer_participants[layer_name]
                total_weight = sum(self.clients[cid].data.weight for cid in participants)
                
                # Zero out parameter before aggregation
                param.data.zero_()
                
                # Aggregate parameters from participating clients
                for client_id in participants:
                    if name in client_models[client_id]:
                        client_weight = self.clients[client_id].data.weight / total_weight
                        param.data.add_(client_models[client_id][name].data * client_weight)
            # else: keep original parameters for this layer as no one participated
        
        

class FedLAMAServer(FLServer):
    """Server implementation for FedLAMA with adaptive layer-wise aggregation."""
    def __init__(self, config: TrainerConfig, globalmodelstate: ModelState):
        super().__init__(config, globalmodelstate)
        self.tau_prime = config.algorithm_params.get('tau_prime', 1)  # Base interval τ'
        self.phi = config.algorithm_params.get('phi', 1)  # Interval increase factor
        self.round = 0
        self.aggregation_intervals = None
        
    def _create_client(self, clientdata, modelstate, personal_model = False):
        """Create a client instance."""
        return FedLAMAClient(
            config=self.config,  
            data=clientdata, 
            modelstate=modelstate.copy(),  
            metrics_calculator=MetricsCalculator(self.config.dataset_name),
            personal_model=personal_model
        )
    
    def calculate_layer_discrepancy(self):
        """Calculate layer-wise model discrepancy across clients."""
        diff_dict = {name: 0.0 for name, _ in self.serverstate.model.named_parameters()}
        layer_dims = {name: param.numel() for name, param in self.serverstate.model.named_parameters()}
        
        for client in self.clients.values():
            client_state = client.get_client_state(personal = False)
            client_model = client_state.model.state_dict()
            for name, global_param in self.serverstate.model.named_parameters():
                client_param = client_model[name]
                diff_dict[name] += torch.norm(global_param - client_param).item()
                
        # Normalize by number of clients
        discrepancies = {
            name: diff/len(self.clients) for name, diff in diff_dict.items()
        }
        
        return discrepancies, layer_dims

    def find_aggregation_cutoff(self, sorted_discrepancies, layer_dims):
        """Find the optimal cutoff point l where δ_l ≈ 1-λ_l."""
        total_discrepancy = sum(d * layer_dims[layer] for layer, d in sorted_discrepancies)
        total_size = sum(layer_dims.values())
        
        best_l = 0
        min_diff = float('inf')
        
        cumulative_disc = 0
        cumulative_size = 0
        
        # For each possible cutoff point
        for i, (layer_name, disc) in enumerate(sorted_discrepancies):
            cumulative_disc += disc * layer_dims[layer_name]
            cumulative_size += layer_dims[layer_name]
            
            # Calculate δ_l and λ_l
            delta_l = cumulative_disc / total_discrepancy
            lambda_l = cumulative_size / total_size
            
            # Find point where δ_l is closest to 1-λ_l
            diff = abs(delta_l - (1 - lambda_l))
            if diff < min_diff:
                min_diff = diff
                best_l = i + 1
                
        return best_l

    def adjust_aggregation_intervals(self):
        """Adjust aggregation intervals based on layer discrepancy."""
        # Get discrepancies and dimensions
        discrepancies, layer_dims = self.calculate_layer_discrepancy()
        # Sort layers by discrepancy
        sorted_layers = sorted(discrepancies.items(), key=lambda x: x[1])
        
        # Find optimal cutoff point
        cutoff_l = self.find_aggregation_cutoff(sorted_layers, layer_dims)
        
        # Set intervals based on cutoff
        new_intervals = {}
        for i, (layer_name, _) in enumerate(sorted_layers):
            # Layers before cutoff get increased interval
            if i < cutoff_l:
                new_intervals[layer_name] = self.phi * self.tau_prime
            else:
                new_intervals[layer_name] = self.tau_prime
                
        return new_intervals

    def aggregate_models(self):
        """Aggregate with adaptive intervals."""
        # Initialize intervals if not set
        if self.aggregation_intervals is None:
            self.aggregation_intervals = {
                name: self.tau_prime 
                for name, _ in self.serverstate.model.named_parameters()
            }
        
        # Update intervals periodically
        if self.round == 2 or (self.round + 1) % (self.phi * self.tau_prime) == 0:
            self.aggregation_intervals = self.adjust_aggregation_intervals()

        # Create new state dict for aggregation
        new_state = self.serverstate.model.state_dict()

        # Aggregate only layers due for synchronization
        for name, param in new_state.items():
            if self.round < 2 or (self.round % self.aggregation_intervals[name] == 0):
                param.data.zero_()
                for client in self.clients.values():
                    client_state = client.get_client_state(personal = False)
                    client_model = client_state.model.state_dict()
                    param.data.add_(
                        client_model[name].data * client.data.weight
                    )
        self.round += 1


class pFedLAServer(FLServer):
    """pFedLA server implementation with layer-wise personalized aggregation."""
    def __init__(self, config: TrainerConfig, globalmodelstate: ModelState):
        super().__init__(config, globalmodelstate)
        # pFedLA specific parameters from config
        algorithm_params = config.algorithm_params
        self.embedding_dim = algorithm_params.get('embedding_dim', 32)
        self.hidden_dim = algorithm_params.get('hidden_dim', 64)
        self.hn_lr = algorithm_params.get('hn_lr', 0.01)
       
    def _initialize_hypernetwork(self):
        """Initialize hypernetwork and related attributes when all clients are ready."""
        # Initialize hypernetwork
        self.hypernetwork = HyperNetwork(
            embedding_dim=self.embedding_dim,
            client_num=len(self.clients),
            hidden_dim=self.hidden_dim,
            backbone=self.serverstate.model,
        ).to(self.device)
        
        # Initialize per-client models - explicitly move to same device as hypernetwork
        self.client_models = [
            [param.clone().detach().to(self.device) for param in self.serverstate.model.parameters()]
            for _ in range(len(self.clients))
        ]
        
        # Track parameter names
        self.layer_names = [name for name, _ in self.serverstate.model.named_parameters()]
        self.trainable_names = [
            name for name, param in self.serverstate.model.named_parameters()
            if param.requires_grad
        ]
    
    def _create_client(self, clientdata, modelstate, personal_model = False):
        """Create a client instance."""
        return pFedLAClient(
            config=self.config,  
            data=clientdata, 
            modelstate=modelstate.copy(),  
            metrics_calculator=MetricsCalculator(self.config.dataset_name),
            personal_model=personal_model
        )
    
    def add_client(self, clientdata: SiteData):
        """Override add_client to initialize hypernetwork after adding client."""
        super().add_client(clientdata)
        if len(self.clients) == self.config.num_clients:
            self._initialize_hypernetwork()

    def generate_client_model(self, client_id):
        """Generate personalized model for client using hypernetwork."""
        alpha = self.hypernetwork(client_id)
        # Stack client parameters for efficient computation
        layer_params = {}
        for name, params in zip(self.layer_names, zip(*self.client_models)):
            # Ensure all params are on the same device before stacking
            params = [p.to(self.device) for p in params]
            layer_params[name] = torch.stack(params, dim=0)
        
        personalized_params = OrderedDict()
        for name in self.layer_names:
            if name in self.trainable_names:
                base_name = name.split('.')[0]
                weights = alpha[base_name].to(self.device)  # Ensure weights are on correct device
            else:
                weights = torch.zeros(len(self.clients), device=self.device)
                weights[client_id] = 1.0
                
            weights = weights / weights.sum() if weights.sum() != 0 else torch.ones_like(weights) / len(weights)
            
            # Handle different parameter shapes
            param_shape = layer_params[name].shape
            if len(param_shape) == 5:  # Conv2d weights
                weights_expanded = weights.view(-1, 1, 1, 1, 1).expand(-1, *param_shape[1:])
            elif len(param_shape) == 3:  # Linear weights
                weights_expanded = weights.view(-1, 1, 1).expand(-1, *param_shape[1:])
            elif len(param_shape) == 2:  # Linear weights without batch dim
                weights_expanded = weights.view(-1, 1).expand(-1, param_shape[1])
            
            personalized_params[name] = torch.sum(
                weights_expanded * layer_params[name],
                dim=0
            )
        
        return personalized_params
    
    def update_hypernetwork(self, client_id, delta, retained_layers=None):
        """Update hypernetwork parameters using client updates."""
        client_idx = int(str.split(client_id, 'client_')[-1]) - 1
        retained_layers = retained_layers or []
        
        # Ensure delta is on the correct device
        delta = {name: param.to(self.device) for name, param in delta.items()}
        
        update_params = [
            param for name, param in delta.items()
            if name in self.trainable_names and name.split('.')[0] not in retained_layers
        ]
        
        if not update_params:
            return
            
        hn_grads = torch.autograd.grad(
            outputs=list(filter(
                lambda p: p.requires_grad,
                self.client_models[client_idx]
            )),
            inputs=self.hypernetwork.get_params(),
            grad_outputs=update_params,
            allow_unused=True
        )
        
        for param, grad in zip(self.hypernetwork.get_params(), hn_grads):
            if grad is not None:
                param.data -= self.hn_lr * grad

    def update_client_model(self, client_id, delta):
        """Update stored client model parameters."""
        updated_params = []
        client_idx = int(str.split(client_id, 'client_')[-1]) - 1
        
        # Ensure delta is on the correct device
        delta = {name: param.to(self.device) for name, param in delta.items()}
        
        for param, diff in zip(self.client_models[client_idx], delta.values()):
            updated_params.append((param + diff).detach())  # Keep on current device
        self.client_models[client_idx] = updated_params

    def train_round(self):
        """Override train_round to implement pFedLA training."""
        train_loss = 0
        val_loss = 0
        val_score = {}
        start_time = time.time()
        for client_id in self.clients:
            client = self.clients[client_id]
            
            # Generate and distribute personalized model
            personalized_params = self.generate_client_model(client_id)
            client.set_model_state(personalized_params)
            
            # Train clients
            client_train_loss = client.train(self.personal)
            client_val_loss, client_val_score = client.validate(self.personal)
            
            # Get updates
            delta = client.compute_updates()
            
            # Update client model and hypernetwork
            self.update_client_model(client_id, delta)
            self.update_hypernetwork(client_id, delta)
            
            # Weight metrics
            train_loss += client_train_loss * client.data.weight
            val_loss += client_val_loss * client.data.weight
            val_score = self._aggregate_scores(val_score, client_val_score, client.data.weight)
        mid_time = time.time()
        print(f"Time per train epoch: {mid_time - start_time:.2f} seconds.")
        # Track metrics
        self.serverstate.train_losses.append(train_loss)
        self.serverstate.val_losses.append(val_loss)
        self.serverstate.val_scores.append(val_score)

        # Update best model if improved
        if val_loss < self.serverstate.best_loss:
            self.serverstate.best_loss = val_loss
            self.serverstate.best_model = copy.deepcopy(self.serverstate.model)

        return train_loss, val_loss, val_score


    def aggregate_models(self):
        """Override to prevent default FedAvg aggregation."""
        pass  # pFedLA handles aggregation through hypernetwork

    def distribute_global_model(self):
        """Override to prevent default model distribution."""
        pass  # pFedLA handles model distribution through hypernetwork

