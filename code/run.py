import argparse
from configs import DATASETS, mp
from pipeline import ExperimentType, ExperimentConfig, Experiment



def run_experiments(dataset: str, experiment_type: str):
    """Run experiment based on type"""
    config = ExperimentConfig(dataset=dataset, experiment_type=experiment_type)
    experiment = Experiment(config)
    return experiment.run_experiment()

def main():
    parser = argparse.ArgumentParser(description='Run federated learning experiments')
    parser.add_argument("-ds", "--dataset", required=True, 
                      choices=list(DATASETS),
                      help="Dataset to use for experiments")
    parser.add_argument("-exp", "--experiment_type", required=True,
                      choices=['learning_rate', 'evaluation'],
                      help="Type of experiment to run")

    args = parser.parse_args()
    
    try:
        # Map experiment type string to ExperimentType
        type_mapping = {
            'learning_rate': ExperimentType.LEARNING_RATE,
            'evaluation': ExperimentType.EVALUATION
        }
        experiment_type = type_mapping[args.experiment_type]
        run_experiments(args.dataset, experiment_type)
        
    except Exception as e:
        print(f"Error running experiments: {str(e)}")
        raise

if __name__ == "__main__":
    main()