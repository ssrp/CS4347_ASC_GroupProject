from __future__ import print_function, division

import argparse
import os
import pickle
# Ignore warnings
import warnings

import numpy as np
import torch

warnings.filterwarnings("ignore")

# import PyTorch Functionalities
import torch.nn.functional as F
import torch.nn as nn
from torch.utils.data import Dataset
import torch.optim as optim
from torchvision import transforms, utils
import torch.utils.data

# import Librosa, tool for extracting features from audio data

# Personal imports
from InputGeneration import inputGeneration as ig
from Pytorch.DenseNet.DenseNetPerso import DenseNetPerso
import Pytorch.DenseNet.denseNetParameters as dnp


# Creates a Tensor from the Numpy dataset, which is used by the GPU for processing
class ToTensor(object):
    def __call__(self, sample):
        data, label = sample
        waveform, spectrogram, features, fmstd = data

        data_torch = (
            torch.from_numpy(waveform),
            torch.from_numpy(spectrogram),
            torch.from_numpy(features),
            torch.from_numpy(fmstd),
        )

        return data_torch, torch.from_numpy(label)


# Code for Normalization of the data
class Normalize(object):
    def __init__(
            self,
            mean_waveform, std_waveform,
            mean_spectrogram, std_spectrogram,
            mean_features, std_features,
            mean_fmstd, std_fmstd
    ):
        self.mean_waveform, self.std_waveform = mean_waveform, std_waveform
        self.mean_spectrogram, self.std_spectrogram = mean_spectrogram, std_spectrogram
        self.mean_features, self.std_features = mean_features, std_features
        self.mean_fmstd, self.std_fmstd = mean_fmstd, std_fmstd

    def __call__(self, sample):
        data, label = sample
        waveform, spectrogram, features, fmstd = data

        waveform = (waveform - self.mean_waveform) / self.std_waveform
        spectrogram = (spectrogram - self.mean_spectrogram) / self.std_spectrogram
        features = (features - self.mean_features) / self.std_features
        fmstd = (fmstd - self.mean_fmstd) / self.std_fmstd

        data = waveform, spectrogram, features, fmstd

        return data, label


class DCASEDataset(Dataset):
    def __init__(self, csv_file, root_dir, save_dir, transform=None, light_data=False):
        """
        Args:
            csv_file (string): Path to the csv file with annotations.
            root_dir (string): Directory with all the audio.
            transform (callable, optional): Optional transform to be applied
                on a sample.
        """

        data_list = []
        label_list = []
        label_indices = []
        with open(csv_file, 'r') as f:
            content = f.readlines()
            content = content[2:]
            flag = 0
            for x in content:
                if flag == 0:
                    row = x.split(',')
                    data_list.append(row[0])  # first column in the csv, file names
                    label_list.append(row[1])  # second column, the labels
                    label_indices.append(row[2])  # third column, the label indices (not used in this code)
                    flag = 1
                else:
                    flag = 0
        self.save_dir = save_dir
        self.root_dir = root_dir
        self.transform = transform
        self.datalist = data_list
        self.labels = label_list
        self.default_labels = ['airport', 'bus', 'metro', 'metro_station', 'park', 'public_square', 'shopping_mall',
                               'street_pedestrian', 'street_traffic', 'tram']

        # Test if light training
        self.light_train = light_data
        if self.light_train:
            self.datalist = self.datalist[:20]
            self.labels = self.labels[0:20]

    def __len__(self):
        return len(self.datalist)

    def __getitem__(self, idx):
        wav_name = self.datalist[idx]
        wav_path = os.path.join(self.root_dir, wav_name)
        npy_name = os.path.splitext(os.path.split(wav_name)[1])[0] + '.npy'
        npy_path = os.path.join(
            self.save_dir,
            npy_name
        )

        # load the wav file with 22.05 KHz Sampling rate and only one channel
        # audio, sr = librosa.core.load(wav_name, sr=22050, mono=True)
        data_computed = None
        if os.path.exists(npy_path):
            data_computed = np.load(npy_path)
        else:
            data_computed = ig.getAllInputs(os.path.abspath(wav_path))
            np.save(npy_path, data_computed)

        # extract the label
        label = np.asarray(self.default_labels.index(self.labels[idx]))

        # final sample
        sample = (data_computed, label)

        # perform the transformation (normalization etc.), if required
        if self.transform:
            sample = self.transform(sample)

        return sample


def train(args, model, device, train_loader, optimizer, epoch):
    model.train()

    # training module
    for batch_idx, sample_batched in enumerate(train_loader):

        # for every batch, extract data and label (16, 1)
        data, label = sample_batched
        waveform, spectrogram, features, fmstd = data  # (16, 2, 240000), (16, 2, 1025, 431), (16, 10, 431), (16, 1, 10)

        # Map the variables to the current device (CPU or GPU)
        waveform = waveform.to(device, dtype=torch.float)
        spectrogram = spectrogram.to(device, dtype=torch.float)
        features = features.to(device, dtype=torch.float)
        fmstd = fmstd.to(device, dtype=torch.float)
        label = label.to(device, dtype=torch.long)

        # set initial gradients to zero :
        # https://discuss.pytorch.org/t/why-do-we-need-to-set-the-gradients-manually-to-zero-in-pytorch/4903/9
        optimizer.zero_grad()

        # pass the data into the model
        output = model(
            x_audio=waveform,
            x_spectrum=spectrogram,
            x_features=features,
            x_fmstd=fmstd
        )

        # get the loss using the predictions and the label
        loss = F.nll_loss(output, label)

        # backpropagate the losses
        loss.backward()

        # update the model parameters :
        # https://discuss.pytorch.org/t/how-are-optimizer-step-and-loss-backward-related/7350
        optimizer.step()

        # Printing the results
        if batch_idx % args.log_interval == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                epoch, batch_idx * len(data), len(train_loader.dataset),
                       100. * batch_idx / len(train_loader), loss.item()))


def test(args, model, device, test_loader, data_type):
    # evaluate the model
    model.eval()

    # init test loss
    test_loss = 0
    correct = 0
    print('Testing..')

    # Use no gradient backpropagations (as we are just testing)
    with torch.no_grad():
        # for every testing batch
        for i_batch, sample_batched in enumerate(test_loader):
            # for every batch, extract data (16, 1, 40, 500) and label (16, 1)
            data, label = sample_batched

            # Map the variables to the current device (CPU or GPU)
            data = data.to(device, dtype=torch.float)
            label = label.to(device, dtype=torch.long)

            # get the predictions
            output = model(data)

            # accumulate the batchwise loss
            test_loss += F.nll_loss(output, label, reduction='sum').item()

            # get the predictions
            pred = output.argmax(dim=1, keepdim=True)

            # accumulate the correct predictions
            correct += pred.eq(label.view_as(pred)).sum().item()
    # normalize the test loss with the number of test samples
    test_loss /= len(test_loader.dataset)

    # print the results
    print('Model prediction on ' + data_type + ': Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
        test_loss, correct, len(test_loader.dataset),
        100. * correct / len(test_loader.dataset)))


def NormalizeData(train_labels_dir, root_dir, g_train_data_dir, light_train=False):
    # load the dataset
    dcase_dataset = DCASEDataset(
        csv_file=train_labels_dir,
        root_dir=root_dir,
        save_dir=g_train_data_dir,
        light_data=light_train
    )

    # flag for the first element
    flag = 0

    # concatenate the datas computed inputs
    wavformConcat = np.asarray([])
    spectrogramConcat = np.asarray([])
    featuresConcat = np.asarray([])
    fmstdConcat = np.asarray([])

    # generate a random permutation, because it's fun. there's no specific reason for that.
    rand = np.random.permutation(len(dcase_dataset))

    # for all the training samples
    for i in range(len(dcase_dataset)):

        # extract the sample
        if light_train:
            sample = dcase_dataset[i]
        else:
            sample = dcase_dataset[rand[i]]

        data_computed, label = sample
        wavform, spectrogram, features, fmstd = data_computed
        if flag == 0:
            # get the data and init melConcat for the first time
            wavformConcat = wavform
            spectrogramConcat = spectrogram
            featuresConcat = features
            fmstdConcat = fmstd
            flag = 1
        else:
            # concatenate the features :
            wavformConcat = np.concatenate((wavformConcat, wavform), axis=0)
            spectrogramConcat = np.concatenate((spectrogramConcat, spectrogram), axis=0)
            featuresConcat = np.concatenate((featuresConcat, features), axis=1)
            fmstdConcat = np.concatenate((fmstdConcat, fmstd), axis=0)

        # print because we like to see it working
        print(
            'NORMALIZATION (FEATURE SCALING) : {0}'
            ' - wavform shape : {1}'
            ' - spectrogram shape : {2}'
            ' - features : {3}'
            ' - fmstd : {4}'.format(
                i,
                wavform.shape,
                spectrogram.shape,
                features.shape,
                fmstd.shape
            )
        )
        print(
            'Current accumulation size :'
            ' - wavformConcat shape : {0}'
            ' - spectrogramConcat shape : {1}'
            ' - featuresConcat : {2}'
            ' - fmstdConcat : {3}'.format(
                wavformConcat.shape,
                spectrogramConcat.shape,
                featuresConcat.shape,
                fmstdConcat.shape
            )
        )

    # extract std and mean
    wavform_mean = np.array([np.mean(wavformConcat)])
    wavform_std = np.array([np.std(wavformConcat)])

    spectrogram_mean = np.mean(spectrogramConcat, axis=(0, 2))
    spectrogram_std = np.std(spectrogramConcat, axis=(0, 2))

    featuresConcat = np.reshape(featuresConcat, (5, 2, -1))     # (5, 2, 8911)
    features_mean = np.mean(featuresConcat, axis=(1, 2))
    features_std = np.std(featuresConcat, axis=(1, 2))

    fmstd_mean = np.mean(fmstdConcat, axis=0)
    fmstd_std = np.std(fmstdConcat, axis=0)

    normalization_values = {
        'waveform': (wavform_mean, wavform_std),
        'spectrogram': (spectrogram_mean, spectrogram_std),
        'features': (features_mean, features_std),
        'fmstd': (fmstd_mean, fmstd_std)
    }

    return normalization_values


def main():
    # Training settings
    parser = argparse.ArgumentParser(description='PyTorch Baseline code for ASC Group Project (CS4347)')
    parser.add_argument('--batch-size', type=int, default=16, metavar='N',
                        help='input batch size for training (default: 16)')
    parser.add_argument('--test-batch-size', type=int, default=16, metavar='N',
                        help='input batch size for testing (default: 16)')
    parser.add_argument('--epochs', type=int, default=200, metavar='N',
                        help='number of epochs to train (default: 200)')
    parser.add_argument('--lr', type=float, default=0.01, metavar='LR',
                        help='learning rate (default: 0.001)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--seed', type=int, default=1, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                        help='how many batches to wait before logging training status')
    parser.add_argument('--save-model', action='store_true', default=False,
                        help='For Saving the current Model')

    parser.add_argument('--light-train', action='store_true', default=False,
                        help='For training on a small number of data')
    parser.add_argument('--light-test', action='store_true', default=False,
                        help='For testing on a small number of data')

    args = parser.parse_args()
    use_cuda = not args.no_cuda and torch.cuda.is_available()

    torch.manual_seed(args.seed)

    device = torch.device("cuda" if use_cuda else "cpu")

    # init the train and test directories
    train_labels_dir = 'Dataset/train/train_labels.csv'
    test_labels_dir = 'Dataset/test/test_labels.csv'
    train_data_dir = 'Dataset/train/'
    test_data_dir = 'Dataset/test/'

    ##### Creation of the folders for the Generated Dataset #####

    light_train = args.light_train
    light_test = args.light_test
    light_train = True
    light_test = True
    if light_train:
        # If we want to test on CPU
        ig.setLightEnviromnent()
        g_train_data_dir = './GeneratedLightDataset/train/'
        g_test_data_dit = './GeneratedLightDataset/test/'
        g_data_dir = './GeneratedLightDataset/'
    else:
        ig.setEnviromnent()
        g_train_data_dir = './GeneratedDataset/train/'
        g_test_data_dit = './GeneratedDataset/test/'
        g_data_dir = './GeneratedDatase/'

    if os.path.isfile(
            os.path.join(g_data_dir, 'normalization_values.npy')
    ) and os.path.isfile(
        os.path.join(g_data_dir, 'input_parameters.p')
    ):
        # get the mean and std. If Normalized already, just load the npy files and comment
        #  the NormalizeData() function above
        normalization_values = np.load(os.path.join(g_data_dir, 'normalization_values.npy'))
        normalization_values = normalization_values.item()      # We have to do this to access the dictionary
        input_parameters = None
        with open(os.path.join(g_data_dir, 'input_parameters.p'), 'rb') as dump_file:
            input_parameters = pickle.load(dump_file)
        print(
            'LOAD OF THE FILE normalization_values.npy FOR NORMALIZATION AND input_parameters.p FOR THE NEURAL NETWORK'
        )
    else:
        # If not, run the normalization and save the mean/std
        print('DATA NORMALIZATION : ACCUMULATING THE DATA')
        normalization_values = NormalizeData(
            train_labels_dir,
            train_data_dir,
            g_train_data_dir=g_train_data_dir,
            light_train=light_train
        )
        np.save(os.path.join(g_data_dir, 'normalization_values.npy'), normalization_values)
        ig.returnInputParameters(
            template=dnp.input_parameters,
            fileName=os.path.abspath(os.path.join(g_data_dir, 'input_parameters.p'))
        )
        with open(os.path.join(g_data_dir, 'input_parameters.p'), 'rb') as dump_file:
            input_parameters = pickle.load(dump_file)

        print('DATA NORMALIZATION COMPLETED')

    # Load of the values in the file
    waveform_mean, waveform_std = normalization_values['waveform']  # (1,), (1,)
    spectrogram_mean, spectrogram_std = normalization_values['spectrogram']  # (1025,), (1025,)
    features_mean, features_std = normalization_values['features']  # (5,), (5,)
    fmstd_mean, fmstd_std = normalization_values['fmstd']       # (10,), (10,)

    # Create the good shape for applying operations to the tensor
    waveform_mean = np.concatenate([waveform_mean, waveform_mean])[:, np.newaxis]  # (2, 1)
    waveform_std = np.concatenate([waveform_std, waveform_std])[:, np.newaxis]  # (2, 1)
    spectrogram_mean = np.concatenate([spectrogram_mean[:, np.newaxis], spectrogram_mean[:, np.newaxis]],
                                      axis=1).T[:, :, np.newaxis]  # (2, 1025, 1)
    spectrogram_std = np.concatenate([spectrogram_std[:, np.newaxis], spectrogram_std[:, np.newaxis]],
                                     axis=1).T[:, :, np.newaxis]  # (2, 1025, 1)
    features_mean = np.reshape(  # (10, 1)
        np.concatenate(
            [
                features_mean[:, np.newaxis],
                features_mean[:, np.newaxis]
            ]
        ),
        (10, 1)
    )
    features_std = np.reshape(  # (10, 1)
        np.concatenate(
            [
                features_std[:, np.newaxis],
                features_std[:, np.newaxis]
            ]
        ),
        (10, 1)
    )
    fmstd_mean = fmstd_mean[np.newaxis, :]  # (1, 10)
    fmstd_std = fmstd_std[np.newaxis, :]    # (1, 10)

    # convert to torch variables
    waveform_mean, waveform_std = torch.from_numpy(waveform_mean), torch.from_numpy(waveform_std)
    spectrogram_mean, spectrogram_std = torch.from_numpy(spectrogram_mean), torch.from_numpy(spectrogram_std)
    features_mean, features_std = torch.from_numpy(features_mean), torch.from_numpy(features_std)
    fmstd_mean, fmstd_std = torch.from_numpy(fmstd_mean), torch.from_numpy(fmstd_std)

    # init the data_transform
    data_transform = transforms.Compose([
        ToTensor(), Normalize(
            waveform_mean, waveform_std,
            spectrogram_mean, spectrogram_std,
            features_mean, features_std,
            fmstd_mean, fmstd_std
        )
    ])


    # init the datasets
    dcase_dataset = DCASEDataset(
        csv_file=train_labels_dir,
        root_dir=train_data_dir,
        save_dir=g_train_data_dir,
        transform=data_transform,
        light_data=light_train
    )
    dcase_dataset_test = DCASEDataset(
        csv_file=test_labels_dir,
        root_dir=test_data_dir,
        save_dir=g_test_data_dit,
        transform=data_transform,
        light_data=light_test
    )

    # set number of cpu workers in parallel
    kwargs = {'num_workers': 16, 'pin_memory': True} if use_cuda else {}


    # get the training and testing data loader
    train_loader = torch.utils.data.DataLoader(
        dcase_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        **kwargs
    )

    test_loader = torch.utils.data.DataLoader(
        dcase_dataset_test,
        batch_size=args.test_batch_size,
        shuffle=False,
        **kwargs
    )

    # init the model
    model = DenseNetPerso(
        dn_parameters=dnp.dn_parameters,
        input_parameters=input_parameters,
    ).to(device)

    # init the optimizer
    optimizer = optim.Adam(model.parameters(), lr=args.lr)


    print('MODEL TRAINING START')
    # train the model
    for epoch in range(1, args.epochs + 1):
        train(args, model, device, train_loader, optimizer, 2)
        test(args, model, device, train_loader, 'Training Data')
        test(args, model, device, test_loader, 'Testing Data')

    print('MODEL TRAINING END')
    """
    # save the model
    if args.save_model:
        torch.save(model.state_dict(), "BaselineASC.pt")
    """

if __name__ == '__main__':
    # create a separate main function because original main function is too mainstream
    main()
