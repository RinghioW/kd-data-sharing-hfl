import torch
import torchvision
from statistics import fmean
import time

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
class Server():
    def __init__(self, model, users, logger, dataset="cifar10"):
        self.model = model
        torch.save({'model_state_dict': model().state_dict()}, "checkpoints/server.pt")
        self.dataset = dataset
        self.users = users
        self.wall_clock_training_times = [0.] * len(users)
        self.scaling_factor = 0.5
        self.init = False
        self.logger = logger

    # Aggregate the updates from the users
    # In this case, averaging the weights will be sufficient
    # Step 18 in the ShuffleFL algorithm
    def _aggregate_updates(self):
        # Load the first user model
        state_dicts = [torch.load(f"checkpoints/user_{i}.pt")['model_state_dict'] for i in range(len(self.users))]
        n_samples = [user.n_samples() for user in self.users]
        total_samples = sum(n_samples)
        avg_state_dict = {}
        for key in state_dicts[0].keys():
            avg_state_dict[key] = sum([state_dict[key] * n_samples[i] for i, state_dict in enumerate(state_dicts)]) / total_samples
        # Save the aggregated weights
        torch.save({'model_state_dict': avg_state_dict}, "checkpoints/server.pt")

    # Evaluate the server model on the test set
    def test(self, testset):
        to_tensor = torchvision.transforms.ToTensor()
        testset = testset.map(lambda img: {"img": to_tensor(img)}, input_columns="img").with_format("torch")
        testloader = torch.utils.data.DataLoader(testset, batch_size=32, num_workers=3)
        net = self.model()
        checkpoint = torch.load("checkpoints/server.pt")
        net.load_state_dict(checkpoint['model_state_dict'])
        if torch.cuda.is_available():
            net = net.to(DEVICE)
        net.eval()
        
        """Evaluate the network on the entire test set."""
        criterion = torch.nn.CrossEntropyLoss()
        correct, total, loss = 0, 0, 0.0
        with torch.no_grad():
            for batch in testloader:
                images, labels = batch["img"].to(DEVICE), batch["label"].to(DEVICE)
                outputs = net(images)
                loss += criterion(outputs, labels).item()
                _, predicted = torch.max(outputs.data, 1)
                total += labels.size(0)
                correct += (predicted == labels).sum().item()
        loss /= len(testloader.dataset)
        accuracy = correct / total

        self.logger.s_log_test(accuracy, loss)
        return loss, accuracy
    
    # Equation 10 in ShuffleFL
    def _send_adaptive_scaling_factor(self):
        # Compute estimated performances of the users
        estimated_performances = [user.diff_capability() * training_time for user, training_time in zip(self.users, self.wall_clock_training_times)]

        # Compute average user performance
        avg_user_performance = fmean(estimated_performances)

        # Compute adaptive scaling factor for each user
        for user, performance in zip(self.users, estimated_performances):
            user.adaptive_scaling_factor = (avg_user_performance / performance) * self.scaling_factor

    # Select users for the next round of training
    def _select_users(self):
        # TODO: Select users
        pass

        for user in self.users:
            user.model = self.model
            user.logger = self.logger


    def _poll_users(self, kd_epochs, on_device_epochs):
        for user_id, user in enumerate(self.users):
            # User trains devices
            # ShuffleFL step 11-15
            initial_time = time.time()
            user.train(kd_epochs, on_device_epochs)
            self.wall_clock_training_times[user_id] = time.time() - initial_time
            self.logger.u_log_test(user_id, user.validate())

    def train(self):
        print("SERVER: Selecting users")
        # Choose the users for the next round of training
        self._select_users()

        # Log new epoch
        self.logger.new_epoch(len(self.users))

        # Send the adaptive scaling factor to the users
        if self.init:
            self._send_adaptive_scaling_factor()

        print("SERVER: Polling users")
        # Wait for users to send their model
        self._poll_users(kd_epochs=10, on_device_epochs=10)

        print("SERVER: Aggregating updates")
        # Aggregate the updates from the users
        self._aggregate_updates()

        self.logger.s_log_latency(self.wall_clock_training_times)

        if not self.init:
            self.init = True

    def train_no_adaptation_no_shuffle(self):
        # Choose the users for the next round of training
        self._select_users()

        # Log new epoch
        self.logger.new_epoch(len(self.users))

        # Wait for users to send their model
        self._poll_users_no_adaptation_no_shuffle(on_device_epochs=10)

        # Aggregate the updates from the users
        self._aggregate_updates_no_adaptation_no_shuffle()

        self.logger.s_log_latency(self.wall_clock_training_times)

        if not self.init:
            self.init = True

    def _poll_users_no_adaptation_no_shuffle(self, on_device_epochs):
        for user_id, user in enumerate(self.users):
            # User trains devices
            # ShuffleFL step 11-15
            initial_time = time.time()
            user.train_no_adaptation_no_shuffle(on_device_epochs)
            self.wall_clock_training_times[user_id] = time.time() - initial_time
            user.logger.u_log_test(user_id, user.validate())

    def _aggregate_updates_no_adaptation_no_shuffle(self):
        # Load the first user model
        state_dicts = [torch.load(f"checkpoints/user_{i}.pt")['model_state_dict'] for i in range(len(self.users))]
        avg_state_dict = {}
        for key in state_dicts[0].keys():
            avg_state_dict[key] = sum([state_dict[key] for state_dict in state_dicts]) / len(self.users)
        # Save the aggregated weights
        torch.save({'model_state_dict': avg_state_dict}, "checkpoints/server.pt")