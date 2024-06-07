import torch
import torch.nn as nn
import numpy as np
from config import DEVICE, NUM_CLASSES
import math
import random
import torchvision
import datasets
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.cluster import KMeans
from optimize import optimize_transmission_matrices
from metrics import staleness_factor
from shuffle import shuffle_data
class User():
    def __init__(self, id, devices, classes=NUM_CLASSES) -> None:
        self.id = id
        self.devices = devices
        self.kd_dataset: datasets.Dataset = None
        self.model = None

        # SHUFFLE-FL

        # Transition matrix of ShuffleFL of size (floor{number of classes * shrinkage ratio}, number of devices + 1)
        # The additional column is for the kd_dataset
        # Also used by equation 7 as the optimization variable for the argmin
        # Shrinkage ratio for reducing the classes in the transition matrix
        self.shrinkage_ratio = 0.3

        self.transition_matrices = None
        
        # System latencies for each device
        self.adaptive_scaling_factor = 1.0

        # Staleness factor
        self.staleness_factor = 0.0

        # Average capability beta
        self.diff_capability = 1. + random.random()
        self.average_power = 1. + random.random()
        self.average_bandwidth = 1. + random.random()

        self.init = False


    def __repr__(self) -> str:
        return f"User(id: {self.id}, devices: {self.devices})"

    # Adapt the model to the devices
    # Implements the adaptation step from ShuffleFL Novelty
    # Constructs a function s.t. device_model = f(user_model, device_resources, device_data_distribution)
    def _adapt_model(self, model):
        self.model = model
        for device in self.devices:
            # If compute is low, better to quantize the network
            # If memory is low, better to prune the network
            # If communication is low, does not really matter as to the network
            # If energy is low, better to prune the network
            device.model = model
            device.path = f"checkpoints/user_{self.id}/"
            resources = device.config["compute"] + device.config["memory"] + device.config["energy_budget"]
            # Adaptation is based on the device resources
            if resources < 30:
                device.model_params = {"quantize": True, "pruning_factor": 0.}
            elif resources < 40:
                device.model_params = {"quantize": False, "pruning_factor": 0.5}
            elif resources < 50:
                device.model_params = {"quantize": False, "pruning_factor": 0.3}
            else:
                device.model_params = {"quantize": False, "pruning_factor": 0.1}
    
    # Train the user model using knowledge distillation
    def _aggregate_updates(self, learning_rate=0.001, epochs=10, T=2, soft_target_loss_weight=0.25, ce_loss_weight=0.75):
        student = self.model()
        optimizer = torch.optim.Adam(student.parameters(), lr=learning_rate)
        if self.init:
            checkpoint = torch.load(f"checkpoints/user_{self.id}/user.pt")
            student.load_state_dict(checkpoint['model_state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        else:
            self.init = True
        student.train()

        # TODO : Train in parallel, not sequentially (?)
        teachers = [] 
        for device in self.devices:
            teacher = device.model()
            checkpoint = torch.load(device.path + f"device_{device.config['id']}.pt")
            teacher.load_state_dict(checkpoint['model_state_dict'], strict=False, assign=True)
            teacher.eval()
            teachers.append(teacher)
        
        to_tensor = torchvision.transforms.ToTensor()
        train_loader = torch.utils.data.DataLoader(self.kd_dataset.map(lambda img: {"img": to_tensor(img)}, input_columns="img").with_format("torch"), shuffle=True, batch_size=32, num_workers=2)
        ce_loss = torch.nn.CrossEntropyLoss()
        
        for epoch in range(epochs):

            running_loss = 0.0
            running_kd_loss = 0.0
            running_ce_loss = 0.0
            running_accuracy = 0.0
            for batch in train_loader:
                inputs, labels = batch["img"].to(DEVICE), batch["label"].to(DEVICE)

                optimizer.zero_grad()

                # Forward pass with the teacher model - do not save gradients here as we do not change the teacher's weights
                with torch.no_grad():
                    # Keep the teacher logits for the soft targets
                    teacher_logits = []
                    for teacher in teachers:
                        logits = teacher(inputs)
                        teacher_logits.append(logits)

                # Forward pass with the student model
                student_logits = student(inputs)

                #Soften the student logits by applying softmax first and log() second
                # Compute the mean of the teacher logits received from all devices
                # TODO: Does the mean make sense?
                averaged_teacher_logits = torch.mean(torch.stack(teacher_logits), dim=0)
                soft_targets = torch.nn.functional.softmax(averaged_teacher_logits / T, dim=-1)
                soft_prob = torch.nn.functional.log_softmax(student_logits / T, dim=-1)

                # Calculate the soft targets loss. Scaled by T**2 as suggested by the authors of the paper "Distilling the knowledge in a neural network"
                soft_targets_loss = -torch.sum(soft_targets * soft_prob) / soft_prob.size()[0] * (T**2)

                # Calculate the true label loss
                label_loss = ce_loss(student_logits, labels)

                # Weighted sum of the two losses
                loss = soft_target_loss_weight * soft_targets_loss + ce_loss_weight * label_loss

                loss.backward()
                optimizer.step()

                running_loss += loss.item()
                running_kd_loss += soft_targets_loss.item()
                running_ce_loss += label_loss.item()
                running_accuracy += (torch.max(student_logits, 1)[1] == labels).sum().item()
            print(f"U{self.id}, e{epoch+1} - Loss: {(running_loss / len(train_loader.dataset)): .4f}, Accuracy: {(running_accuracy / len(train_loader.dataset)): .3f}")
        
        # Save the model for checkpointing
        torch.save({'model_state_dict': student.state_dict(), 'optimizer_state_dict': optimizer.state_dict()}, f"checkpoints/user_{self.id}/user.pt")

    # Train all the devices belonging to the user
    # Steps 11-15 in the ShuffleFL Algorithm
    def train(self, epochs=5, verbose=True):
        # Adapt the model
        self._adapt_model(self.model)

        # Train the devices
        for device in self.devices:
            device.train(epochs, verbose)
        
        # Create the knowledge distillation dataset
        self._create_kd_dataset()

        # Aggregate the updates
        self._aggregate_updates()

    # Implements section 4.4 from ShuffleFL
    def _reduce_dimensionality(self):
        lda_estimator, kmeans_estimator = self._compute_centroids(self.shrinkage_ratio)
        for device in self.devices:
            device = device.cluster(lda_estimator, kmeans_estimator)

    def shuffle(self):
        # Reduce dimensionality of the transmission matrices
        # ShuffleFL step 7, 8
        self._reduce_dimensionality()

        # User optimizes the transmission matrices
        # ShuffleFL step 9
        n_devices = len(self.devices)
        n_clusters = math.floor(NUM_CLASSES * self.shrinkage_ratio)
        adaptive_scaling_factor = self.adaptive_scaling_factor
        cluster_distributions = [device.cluster_distribution() for device in self.devices]
        uplinks = [device.config["uplink_rate"] for device in self.devices]
        downlinks = [device.config["downlink_rate"] for device in self.devices]
        computes = [device.config["compute"] for device in self.devices]
        self.transition_matrices = optimize_transmission_matrices(self.transition_matrices, n_devices, n_clusters, adaptive_scaling_factor, cluster_distributions, uplinks, downlinks, computes)

        # Shuffle the data and update the transition matrices
        # Implements Equation 1 from ShuffleFL
        datasets = [device.dataset for device in self.devices]
        clusters = [device.labels for device in self.devices]
        res_datasets, res_clusters = shuffle_data(datasets, clusters, cluster_distributions, self.transition_matrices)

        # Update the devices with the new datasets and clusters
        # TODO: Should clusters be reassigned?
        for d, dataset, cluster in zip(self.devices, res_datasets, res_clusters):
            d.dataset = dataset
            d.clusters = cluster

    # Compute the difference in capability of the user compared to last round
    # Implements Equation 8 from ShuffleFL 
    def update_average_capability(self):
        # Compute current average power and bandwidth and full dataset size
        average_power = sum([device.config["compute"] for device in self.devices]) / len(self.devices)
        average_bandwidth = sum([(device.config["uplink_rate"] + device.config["downlink_rate"]) / 2 for device in self.devices]) / len(self.devices)
        
        # Equation 8 in ShuffleFL
        self.diff_capability = self.staleness_factor * (average_power / self.average_power) + (1. - self.staleness_factor) * (average_bandwidth / self.average_bandwidth)
        
        # Update the average power and bandwidth
        self.average_power = average_power
        self.average_bandwidth = average_bandwidth
    
    # Implements Equation 9 from ShuffleFL
    def compute_staleness_factor(self):
        dataset_size = sum([len(device.dataset) for device in self.devices])
        # TODO : Self should probably keep track of this
        num_transferred_samples = sum([device.num_transferred_samples for device in self.devices])

        # Compute the staleness factor
        self.staleness_factor = staleness_factor(dataset_size, num_transferred_samples)
    
    def _create_kd_dataset(self, percentage=0.2):
        self.kd_dataset = self._sample_devices(percentage)
    
    # TODO: this function should have as parameter the uplink rate of the device
    def _compute_centroids(self, shrinkage_ratio):
        dataset = self._sample_devices(.1)
        features = np.array(dataset["img"]).reshape(len(dataset), -1)
        labels = np.array(dataset["label"])

        lda = LinearDiscriminantAnalysis(n_components=4).fit(features, labels)
        feature_space = lda.transform(features)
        # Cluster datapoints to k classes using KMeans
        n_clusters = math.floor(shrinkage_ratio*NUM_CLASSES)
        kmeans = KMeans(n_clusters=n_clusters).fit(feature_space)

        return lda, kmeans
    
    def _sample_devices(self, percentage):
        # Assemble the entire dataset from the devices
        dataset = []
        for device in self.devices:
            dataset.extend(device.sample(percentage))
        return datasets.Dataset.from_list(dataset)

