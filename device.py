import torch
import math
import datasets
from config import DEVICE, LABEL_NAME, NUM_CLASSES
import numpy as np
from scipy.spatial.distance import jensenshannon
class Device():
    def __init__(self, id, trainset, testset, model) -> None:
        self.id = id
        self.dataset = trainset
        self.testset = testset
        
        self.model = model
        self.compute = 3*(np.exp(id % 5)) * (10**-3)
        self.uplink = (np.exp(id % 5)) * (10**-3)

        self.log = []
        self.clusters = []


    def __repr__(self) -> str:
        return f"Device({self.config}, 'samples': {len(self.dataset)})"
    
    
    def update_model(self, user_model, kd_dataset):

        # Use knowledge distillation to adapt the model to the device
        # Train server model on the dataset using kd
        student = self.model().to(DEVICE)
        student.train()
        optimizer = torch.optim.Adam(student.parameters(), lr=0.001)

        teacher = user_model().to(DEVICE)
        teacher.load_state_dict(torch.load("checkpoints/server.pth"))
        teacher.eval()
        train_loader = torch.utils.data.DataLoader(kd_dataset, shuffle=True, drop_last=True, batch_size=32, num_workers=3)
        ce_loss = torch.nn.CrossEntropyLoss()
        kl_loss = torch.nn.KLDivLoss(reduction="batchmean")
        running_loss = 0.0
        running_accuracy = 0.0
        num_samples = 0
        epochs = 10
        for _ in range(epochs):

            for batch in train_loader:
                inputs, labels = batch["img"].to(DEVICE), batch[LABEL_NAME].to(DEVICE)
                optimizer.zero_grad()

                # Forward pass with the teacher model - do not save gradients here as we do not change the teacher's weights
                with torch.no_grad():
                    # Keep the teacher logits for the soft targets
                    teacher_logits = teacher(inputs)

                # Forward pass with the student model
                student_logits = student(inputs)
                T = 3.0
                soft_targets = torch.nn.functional.softmax(teacher_logits / T, dim=-1)
                soft_prob = torch.nn.functional.log_softmax(student_logits / T, dim=-1)

                # Calculate the soft targets loss. Scaled by T**2 as suggested by the authors of the paper "Distilling the knowledge in a neural network"
                soft_targets_loss = kl_loss(soft_prob, soft_targets) * (T ** 2)

                # Calculate the true label loss
                label_loss = ce_loss(student_logits, labels)

                # Weighted sum of the two losses
                soft_target_loss_weight = 0.4
                ce_loss_weight = 0.6
                loss = (soft_target_loss_weight * soft_targets_loss) + (ce_loss_weight * label_loss)

                loss.backward()
                optimizer.step()

                running_loss += loss.item()
                num_samples += labels.size(0)
                running_accuracy += (torch.max(student_logits, 1)[1] == labels).sum().item()
        torch.save(student.state_dict(), f"checkpoints/device_{self.id}.pth")

    # Perform on-device learning on the local dataset. This is simply a few rounds of SGD.
    def train(self, epochs):
        print(f"DEVICE {self.id} - Training")
        if len(self.dataset) == 0:
            return
        
        # Load the model
        net = self.model().to(DEVICE)
        net.load_state_dict(torch.load(f"checkpoints/device_{self.id}.pth"))
        net.train()

        optimizer = torch.optim.Adam(net.parameters(), lr=0.001)

        trainloader = torch.utils.data.DataLoader(self.dataset, batch_size=32, shuffle=True, drop_last=True, num_workers=4)
        criterion = torch.nn.CrossEntropyLoss()

        for _ in range(epochs):
            for batch in trainloader:
                images, labels = batch["img"].to(DEVICE), batch[LABEL_NAME].to(DEVICE)
                optimizer.zero_grad()
                outputs = net(images)
                loss = criterion(outputs, labels)
                loss.backward()
                optimizer.step()
        
        torch.save(net.state_dict(), f"checkpoints/device_{self.id}.pth")
        # self.test()

    def test(self):
        if self.testset is None:
            return 0.
        net = self.model().to(DEVICE)
        net.load_state_dict(torch.load(f"checkpoints/device_{self.id}.pth"))
        net.eval()
        valloader = torch.utils.data.DataLoader(self.testset, batch_size=32, shuffle=False, num_workers=4)
        correct, total = 0, 0
        with torch.no_grad():
            for batch in valloader:
                images, labels = batch["img"].to(DEVICE), batch[LABEL_NAME].to(DEVICE)
                outputs = net(images)
                correct += (torch.max(outputs.data, 1)[1] == labels).sum().item()
                total += labels.size(0)
        print(f"DEVICE {self.id} - Validation Accuracy: {correct / total}")
        self.log.append(correct / total)

    def sample(self, percentage):
        amount = math.floor(percentage * len(self.dataset))
        return datasets.Dataset.shuffle(self.dataset).select([i for i in range(amount)])
    
    def sample_amount(self, amount):
        amount = math.floor(amount)
        return datasets.Dataset.shuffle(self.dataset).select([i for i in range(min(amount, len(self.dataset)))])
    
    # Sample a certain amount of samples from a specific class
    def sample_amount_class(self, amount, class_id):
        amount = math.floor(amount)
        dataset_class = self.dataset.filter(lambda x: x[LABEL_NAME] == class_id)
        return dataset_class.select([i for i in range(min(amount, len(dataset_class)))])
    
    def n_samples(self):
        return len(self.dataset)

    def label_distribution(self):
        return np.bincount(self.dataset[LABEL_NAME], minlength=NUM_CLASSES)

    def imbalance(self):
        if len(self.dataset) == 0:
            return np.float64(0.)
        distribution = self.label_distribution()
        n_samples = sum(distribution)
        n_classes = len(distribution)
        avg_samples = n_samples / n_classes
        balanced_distribution = [avg_samples] * n_classes
        js = jensenshannon(balanced_distribution, distribution)
        return js

    def cluster(self, kmeans_estimator):
        if len(self.dataset) == 0:
            self.clusters = []
            return
        self.clusters = kmeans_estimator.predict(np.array(self.dataset["img"]).reshape(len(self.dataset), -1)).tolist()
    
    def cluster_distribution(self):
        return np.bincount(self.clusters, minlength=5)
    

