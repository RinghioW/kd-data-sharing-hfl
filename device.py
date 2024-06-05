import torch
from config import DEVICE
import numpy as np
import math
import torchvision.transforms as transforms
class Device():
    def __init__(self, config, dataset, valset) -> None:
        self.config = config # Configuration of the device
        self.dataset = dataset

        self.valset = valset # TODO: Figure out how to use this

        self.model = None # Model class (NOT instance)
        self.model_params = None # Configuration to pass to the model constructor
        self.path = None # Relative path to save the model
        self.labels = None # Labels of the dataset
        self.num_transferred_samples = 0
        self.init = False # Useful to figure out whether we have a checkpoint or not

    def __repr__(self) -> str:
        return f"Device({self.config}, 'samples': {len(self.dataset)})"
    
    # Perform on-device learning on the local dataset. This is simply a few rounds of SGD.
    def train(self, epochs=10, verbose=False):

        if len(self.dataset) == 0:
            return

        print(f"Device {self.config['id']} - Training on {len(self.dataset)} samples")
        # Load the model
        net = self.model(**self.model_params)
        optimizer = torch.optim.Adam(net.parameters())

        if self.init:
            checkpoint = torch.load(self.path + f"device_{self.config['id']}.pt")
            net.load_state_dict(checkpoint['model_state_dict'], strict=False, assign=True)
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        else:
            self.init = True
        
        net.train()

        to_tensor = transforms.ToTensor()
        dataset = self.dataset.map(lambda img: {"img": to_tensor(img)}, input_columns="img").with_format("torch")
        trainloader = torch.utils.data.DataLoader(dataset, batch_size=32, shuffle=True)
        """Train the network on the training set."""
        criterion = torch.nn.CrossEntropyLoss()

        for epoch in range(epochs):
            correct, total, epoch_loss = 0, 0, 0.0
            for batch in trainloader:
                images, labels = batch["img"].to(DEVICE), batch["label"].to(DEVICE)
                optimizer.zero_grad()
                outputs = net(images)
                loss = criterion(outputs, labels)
                loss.backward()
                optimizer.step()
                # Metrics
                epoch_loss += loss
                total += labels.size(0)
                correct += (torch.max(outputs.data, 1)[1] == labels).sum().item()
            epoch_loss /= len(trainloader.dataset)
            epoch_acc = correct / total
            if verbose:
                print(f"Device {self.config['id']} - Epoch {epoch+1}: loss {epoch_loss}, accuracy {epoch_acc}")

        # Save the model
        torch.save({
            'model_state_dict': net.state_dict(),
            'optimizer_state_dict': optimizer.state_dict()
        }, self.path + f"device_{self.config['id']}.pt")

    
    # Function to sample a sub-dataset from the dataset
    def sample(self, percentage=None, uplink=None, cluster=None, random=None, n=None):
        dataset = list(self.dataset)
        if percentage is not None:
            amount = math.floor(percentage * len(dataset))
            if uplink is not None:
                pass
        elif cluster is not None:
            n_class_samples = self.dataset_distribution()[cluster]
            amount = math.floor(percentage * n_class_samples)
        elif n is not None:
            amount = n
        if random is not None:
            reduced_dataset = np.random.permutation(dataset)[:amount]
        else:
            reduced_dataset = dataset[:amount]
        return reduced_dataset

    def cluster_data(self, lda_estimator, kmeans_estimator):
        dataset = np.array(self.dataset["img"]).reshape(len(self.dataset), -1)
        feature_space = lda_estimator.transform(dataset)
        self.labels = kmeans_estimator.predict(feature_space).tolist()
    
    def dataset_distribution(self):
        return np.bincount(self.labels).tolist()
    
    @staticmethod
    def generate_configs(num_devices):
        return [{"id" : i,
                "compute" : np.random.randint(10**0, 10**1), # Compute capability in FLOPS
                "memory" : np.random.randint(10**0, 10**1), # Memory capability in Bytes
                "energy_budget" : np.random.randint(10**0,10**1), # Energy budget in J/hour
                "uplink_rate" : np.random.randint(10**0,10**1), # Uplink rate in Bps
                "downlink_rate" : np.random.randint(10**0,10**1) # Downlink rate in Bps
                } for i in range(num_devices)]