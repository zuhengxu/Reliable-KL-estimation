import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import pickle
import matplotlib.pyplot as plt
import seaborn as sns
from utils import *
from estimators import estimate_mutual_information
device = 'cuda' if torch.cuda.is_available() else 'cpu'
sample_size=8

def save_dict(filename, dict):
    file=filename+ '.pkl'
    f = open(file, "wb")
    pickle.dump(dict, f)
    f.close()

def fill_lower_diag_batch(px, n, ncls):
    triu_indices = torch.triu_indices(n, n)
    out = torch.zeros(ncls,n, n).to(device)
    for i in range(ncls):
        out[i,triu_indices[0], triu_indices[1]] = px[i,:]
    return out.permute(0,2,1)

def average_pred(phi, mu, L, sample_size):
    batch_size=phi.shape[0]
    mu=mu.unsqueeze(2).expand(-1,-1,sample_size)
    eps = torch.randn_like(mu)
    eta = torch.bmm(L, eps)
    w=mu+eta
    w=w.unsqueeze(0).expand(batch_size,-1,-1,-1)
    w=w.permute(3,0,2,1)
    phi=phi.unsqueeze(0).unsqueeze(2)
    phi=phi.expand(sample_size,-1,-1,-1)
    output=torch.bmm(phi.contiguous().view(sample_size*batch_size,phi.shape[2],phi.shape[3]), w.contiguous().view(sample_size*batch_size,w.shape[2],w.shape[3]))
    output=output.squeeze()
    output=output.view(sample_size,batch_size,-1)

    return output.mean(0)

dim = 20

# define the training procedure

CRITICS = {
    'separable': SeparableCritic,
    'concat': ConcatCritic,
    'concat_lip_rkhs': ConcatLipRKHS,
    'separable_lip_rkhs': SeparableLipRKHS,

}

BASELINES = {
    'constant': lambda: None,
    'unnormalized': lambda: mlp(dim=dim, hidden_dim=512, output_dim=1, layers=2, activation='relu').cuda(),
    'gaussian': lambda: log_prob_gaussian,
}


def train_estimator_rkhs(critic_params, data_params, mi_params, opt_params, **kwargs):
    """Main training loop that estimates time-varying MI."""
    # Ground truth rho is only used by conditional critic
    critic = CRITICS[mi_params.get('critic')](
        rho=None, **critic_params).cuda()
    # baseline = BASELINES[mi_params.get('baseline', 'constant')]()

    opt_crit = optim.Adam(critic.parameters(), lr=opt_params['learning_rate'])
    # if isinstance(baseline, nn.Module):
    #     opt_base = optim.Adam(baseline.parameters(),
    #                           lr=opt_params['learning_rate'])
    # else:
    #     opt_base = None

    def train_step(rho, data_params, mi_params):
        # Annoying special case:
        # For the true conditional, the critic depends on the true correlation rho,
        # so we rebuild the critic at each iteration.
        opt_crit.zero_grad()
        # if isinstance(baseline, nn.Module):
        #     opt_base.zero_grad()

        # if mi_params['critic'] == 'conditional':
        #     critic_ = CRITICS['conditional'](rho=rho).cuda()
        # else:
        critic_ = critic

        x, y = sample_correlated_gaussian(
            dim=data_params['dim'], rho=rho, batch_size=data_params['batch_size'], cubic=data_params['cubic'])
        # mi = estimate_mutual_information(
        #     mi_params['estimator'], x, y, critic_, baseline, mi_params.get('alpha_logit', None), **kwargs)
        x, y = x.cuda(), y.cuda()
        n = x.size(0)
        if mi_params['critic'] == 'concat_lip_rkhs':
            phi, mu, p, n_f = critic_(x, y)

            L = fill_lower_diag_batch(p, n_f, 1)
            f = average_pred(phi, mu, L, sample_size).squeeze()
            f = torch.reshape(f, [n, n]).t()
        elif mi_params['critic'] == 'separable_lip_rkhs':
            f = critic_(x,y)

        f_diag = f.diag()
        first_term = torch.mean(F.logsigmoid(f_diag))

        second_term = (torch.sum(F.logsigmoid(-f)) -
                       torch.sum(F.logsigmoid(-f_diag))) / (n * (n - 1.))

        neg_divergence = -(first_term + second_term)
        loss = neg_divergence
        if mi_params['critic'] == 'concat_lip_rkhs':
            loss= loss + (torch.mean(mu ** 2)+torch.sum(L.squeeze()**2)/L.numel())
        loss.backward()
        opt_crit.step()
        # if isinstance(baseline, nn.Module):
        #     opt_base.step()
        mi = torch.mean(f_diag)

        return mi

    # Schedule of correlation over iterations
    mis = mi_schedule(opt_params['iterations'])
    rhos = mi_to_rho(data_params['dim'], mis)

    estimates = []
    for i in range(opt_params['iterations']):
        mi = train_step(rhos[i], data_params, mi_params)
        mi = mi.detach().cpu().numpy()
        estimates.append(mi)

    return np.array(estimates)
## parameters for data, critic and optimization

data_params = {
    'dim': dim,
    'batch_size': 64,
    'cubic': None
}

critic_params = {
    'dim': dim,
    'layers': 2,
    'embed_dim': 32,
    'hidden_dim': 256,
    'activation': 'relu',
    'lip' : 5,
}

opt_params = {
    'iterations': 40000,
    'learning_rate': 5e-4,
}

mi_numpys = dict()
if data_params['cubic']:
    out_file = 'Lip_RKHS_mi_lip_'+str(critic_params['lip'])+'_cubic'
else:
    out_file = 'Lip_RKHS_mi_lip_' + str(critic_params['lip']) + '_gauss'
print(out_file)

for critic_type in ['concat_lip_rkhs']:
    mi_numpys[critic_type] = dict()

    # for estimator in ['infonce', 'nwj', 'js', 'smile']:
    # for estimator in ['lip_rkhs']:
    estimator= 'lip_rkhs'

    mi_params = dict(estimator=estimator, critic=critic_type, baseline=None)
    mis = train_estimator_rkhs(critic_params, data_params, mi_params, opt_params)
    mi_numpys[critic_type][f'{estimator}'] = mis

save_dict(out_file, mi_numpys)

    # estimator = 'smile'
    # for i, clip in enumerate([1.0, 5.0]):
    #     mi_params = dict(estimator=estimator, critic=critic_type, baseline='unnormalized')
    #     mis = train_estimator(critic_params, data_params, mi_params, opt_params, clip=clip)
    #     mi_numpys[critic_type][f'{estimator}_{clip}'] = mis