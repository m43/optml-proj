#!/bin/bash
#SBATCH --chdir /scratch/izar/rajic/nli/src/infersent/src/InferSent/
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=20
#SBATCH --mem=90G
#SBATCH --partition=gpu
#SBATCH --qos=gpu
#SBATCH --gres=gpu:1
#SBATCH --time=10:00:00

#SBATCH -o ./logs/slurm_logs/%x-%j.out

set -e
set -o xtrace
echo PWD:$(pwd)
echo STARTING AT $(date)

# Modules
module purge
module load gcc/9.3.0-cuda
module load cuda/11.0.2

# Environment
source ~/miniconda3/etc/profile.d/conda.sh
conda activate optml

# Run
date
printf "Run configured and environment setup. Gonna run now.\n\n"
python train_nli.py \
  --seed 72 \
  --dataset MNLIMatched \
  --outputmodelname 'S4.16_IS_ds-MNLIMatched_gamma-2.0_seed-72' \
  --outputdir /scratch/izar/rajic/nli/logs/infersent/dataset-MNLIMatched_gamma-2.0_seed-72/ \
  --outputfile /scratch/izar/rajic/nli/logs/infersent/dataset-MNLIMatched_gamma-2.0_seed-72.csv \
  --focal_loss --gamma_focal 2.0 --version 2 \
  --h_loss_weight 0.0 \
  --enc_lstm_dim 512 \
  --optimizer=sgd,lr=0.1 \
  --nonlinear_fc \

echo FINISHED at $(date)

