
# Loop through each element in the array
# for dataset in 'coco' 
for dataset in 'flickr' 
do
    for model in  'clip' 
    # for model in 'blip2' 
    do
        # for algo in 'tome' 'pitome' 'tofu' 'dct' 'diffrate'
        for algo in 'tome' 'pitome' 'tofu' 'dct' 'diffrate'
        do
            # for ratio in '0.925' 
            # for ratio in '0.875' '0.9' '0.925' '0.95' '0.975'
            for ratio in '0.925' 
            do
            echo "running $model $size $algo $ratio."
            sh train_scripts/train_itr_$model.sh $dataset $algo $ratio
            done
        done
        sh train_scripts/train_itr_$model.sh $dataset none 1.0 
    done
done