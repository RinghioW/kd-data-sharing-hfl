import torchvision.models as models
import torch.nn as nn
import torch.optim as optim
import torch
import numpy as np
import time
from config import DEVICE, NUM_CLASSES, STD_CORRECTION
from scipy.optimize import minimize
import math

class User():
    def __init__(self, devices, classes=NUM_CLASSES) -> None:
        self.devices = devices
        self.kd_dataset = None
        self.model = None

        # SHUFFLE-FL

        # Transition matrix of ShuffleFL of size (floor{number of classes * shrinkage ratio}, number of devices + 1)
        # The additional column is for the kd_dataset
        # Also used by equation 7 as the optimization variable for the argmin
        # TODO : Add +1 to the len of devices
        # Shrinkage ratio for reducing the classes in the transition matrix
        self.shrinkage_ratio = 0.3
        self.transition_matrices = [np.ones((math.floor(classes*self.shrinkage_ratio), len(devices)), dtype=int) for _ in devices] # TODO: +1
        
        # System latencies for each device
        self.system_latencies = [0.0 for _ in devices]
        self.adaptive_coefficient = 1.0
        self.data_imbalances = [0.0 for _ in devices]

        # Staleness factor
        self.staleness_factor = 0.0

        # Average capability beta
        self.average_capability = 0.0
        self.average_power = 0.0
        self.average_bandwidth = 0.0
        self.capability_coefficient = 0.0


    # Adapt the model to the devices
    def adapt_model(self, model):
        self.model = model
        for device in self.devices:
            # Adaptation is based on the device resources
            if device.config["compute"] < 5:
                device.model = models.quantization.mobilenet_v2(weights=models.MobileNet_V2_Weights.DEFAULT, quantize=False)
            elif device.config["compute"] < 10:
                device.model = models.quantization.mobilenet_v3_large(weights=models.MobileNet_V3_Large_Weights.DEFAULT, quantize=False)
            else:
                device.model = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.DEFAULT)
    
    # Train the user model using knowledge distillation
    def aggregate_updates(self, learning_rate=0.001, epochs=3, T=2, soft_target_loss_weight=0.25, ce_loss_weight=0.75):
        student = self.model
        for i, device in enumerate(self.devices):
            teacher = device.model
            train_loader = torch.utils.data.DataLoader(self.kd_dataset[i], shuffle=True, batch_size=32)
            ce_loss = nn.CrossEntropyLoss()
            optimizer = optim.Adam(student.parameters(), lr=learning_rate)

            teacher.eval()  # Teacher set to evaluation mode
            student.train() # Student to train mode

            for epoch in range(epochs):
                running_loss = 0.0
                for batch in train_loader:
                    inputs, labels = batch["img"].to(DEVICE), batch["label"].to(DEVICE)

                    optimizer.zero_grad()

                    # Forward pass with the teacher model - do not save gradients here as we do not change the teacher's weights
                    with torch.no_grad():
                        teacher_logits = teacher(inputs)

                    # Forward pass with the student model
                    student_logits = student(inputs)

                    #Soften the student logits by applying softmax first and log() second
                    soft_targets = nn.functional.softmax(teacher_logits / T, dim=-1)
                    soft_prob = nn.functional.log_softmax(student_logits / T, dim=-1)

                    # Calculate the soft targets loss. Scaled by T**2 as suggested by the authors of the paper "Distilling the knowledge in a neural network"
                    soft_targets_loss = -torch.sum(soft_targets * soft_prob) / soft_prob.size()[0] * (T**2)

                    # Calculate the true label loss
                    label_loss = ce_loss(student_logits, labels)

                    # Weighted sum of the two losses
                    loss = soft_target_loss_weight * soft_targets_loss + ce_loss_weight * label_loss

                    loss.backward()
                    optimizer.step()

                    running_loss += loss.item()
                print(f"Epoch {epoch+1}/{epochs}, Loss: {running_loss / len(train_loader.dataset)}")

    # Train all the devices belonging to the user
    # Steps 11-15 in the ShuffleFL Algorithm
    def train_devices(self, epochs=5, verbose=True):
        for device in self.devices:
            device.train(epochs, verbose)
    
    def total_latency_devices(self, epochs):
        # Communication depends on the transition matrix
        t_communication = 0
        for device_idx, device in enumerate(self.devices):
            for data_class_idx, data_class in enumerate(device.transition_matrix):
                for other_device_idx, other_device in enumerate(self.devices):
                    if device_idx != other_device_idx:
                        # Transmitting
                        t_communication += device.transition_matrix[data_class_idx][other_device_idx] * ((1/device.config["uplink_rate"]) + (1/other_device.config["downlink_rate"]))
                        # Receiving
                        t_communication += other_device.transition_matrix[data_class_idx][device_idx] * ((1/device.config["downlink_rate"]) + (1/other_device.config["uplink_rate"]))
        t_computation = 0
        for device in self.devices:
            t_computation += 3 * epochs * len(device.dataset) * device.config["compute"]
        return t_communication + t_computation
    
    def latency_devices(self, epochs):
        for i, device in enumerate(self.devices):
            self.system_latencies[i] = device.latency(devices=self.devices, device_idx=i, epochs=epochs)
        return self.system_latencies
    
    def data_imbalance_devices(self):
        for i, device in enumerate(self.devices):
            self.data_imbalances[i] = device.data_imbalance()
        return self.data_imbalances
    
    def send_data(self, sender, sample_class, receiver_idx, amount):
        sample = sender.remove_data(sample_class, amount)
        receiver = self.devices[receiver_idx]
        receiver.add_data(sample)
        return sample

    # Shuffle data between devices according to the transition matrices
    # Implements the transformation described by Equation 1 from ShuffleFL
    def shuffle_data(self, transition_matrices):
        # TODO: Implement the knowledge distillation dataset
        pass

        # Each device sends data according to the respective transition matrix
        for transition_matrix, device in zip(transition_matrices, self.devices):
            for i in range(len(transition_matrix)):
                for j in range(len(transition_matrix[0])):
                    if i != j:
                        # Send data from class i to device j
                        self.send_data(device, i, j, transition_matrix[i][j])
                    else:
                        # TODO: Elements on the diagonal could be the knowledge distillation dataset
                        pass

    # Function to implement the dimensionality reduction of the transition matrices
    # The data is embedded into a 2-dimensional space using t-SNE
    # The classes are then aggregated into k groups using k-means
    # Implements section 4.4 from ShuffleFL
    def reduce_dimensionality(self):
        for device in self.devices:
            device.cluster_data(self.shrinkage_ratio)

    # Function for optimizing equation 7 from ShuffleFL
    def optimize_transmission_matrices(self):
        # Define the objective function to optimize
        # Takes as an input the transfer matrices
        # Returns as an output the result of Equation 7
        def objective_function(x):
            # Parse args
            transfer_matrices = x.reshape((len(self.devices), math.floor(NUM_CLASSES*self.shrinkage_ratio), len(self.devices)))

            # Store the current status of the devices
            current_data = []
            for device in self.devices:
                current_data.append(device.dataset)

            # Transfer the data according to the matrices
            self.shuffle_data(transfer_matrices)

            # Compute the resulting system latencies and data imbalances
            latencies = self.latency_devices(epochs=1)
            data_imbalances = self.data_imbalance_devices()

            # Restore the original status of the devices
            for i, device in enumerate(self.devices):
                device.dataset = current_data[i]

            # Compute the loss function
            # The factor of 10 was introduced to increase by an order of magnitude the importance of the time std
            # Time std is usually very small and the max time is usually very large
            # But a better approach would be to normalize the values or take the square of the std
            return STD_CORRECTION*np.std(latencies) + np.max(latencies) + self.adaptive_coefficient*np.max(data_imbalances)

        # Define the constraints for the optimization
        # TODO: Wtf is this constraint
        def constraint_row_sum(flat_variables, device_num, class_num):
            # Reshape the flat variables back to their original shapes
            variables = flat_variables.reshape((device_num, class_num, device_num))

            # Calculate the row sums for each matrix and ensure they sum to 1
            row_sums = []
            for x in variables:
                row_sum = np.sum(x, axis=1)
                row_sums.extend(row_sum - 1.)
            return row_sums
        
        # TODO: Wtf is this constraint
        def constraint_matrix_elements(variables):
            return variables
        

        n_device = len(self.devices)
        n_class = math.floor(NUM_CLASSES*self.shrinkage_ratio)
        n_var = n_device * n_class * n_device
        bounds = [(0, 1)] * n_var
        constraints = [{'type': 'eq', 'fun': lambda variables: constraint_row_sum(variables, n_device, n_class)},
                       {'type': 'ineq', 'fun': lambda variables: constraint_matrix_elements(variables)},]
        
        # Run the optimization
        x0 = np.array(self.transition_matrices).flatten()
        result = minimize(objective_function, x0, method='SLSQP', bounds=bounds, constraints=constraints, options={'maxiter': 1000, 'ftol': 1e-06, 'disp': True})
        return result.x, result.fun

    # Compute the average capability of the user compared to last round
    # Implements Equation 8 and Equation 9 from ShuffleFL 
    def update_average_capability(self):
        # Compute current average power and bandwidth and full dataset size
        average_power = 0.
        average_bandwidth = 0.
        dataset_size = 0
        for device in self.devices:
            average_power += device.config["compute"]
            average_bandwidth += (device.config["uplink_rate"] + device.config["downlink_rate"]) / 2
            dataset_size += device.dataset_size
        average_power /= len(self.devices)
        average_bandwidth /= len(self.devices)
        
        # TODO: Compute the number of transferred samples
        num_transferred_samples = 0
        pass

        # Equation 9 in ShuffleFL
        self.capability_coefficient = (3 * dataset_size) / ((3 * dataset_size) + num_transferred_samples)
        
        # Equation 8 in ShuffleFL
        self.average_capability = self.capability_coefficient * (average_power / self.average_power) + (1. - self.capability_coefficient) * (average_bandwidth / self.average_bandwidth)
        # Update the average capability
        self.average_power = average_power
        self.average_bandwidth = average_bandwidth

