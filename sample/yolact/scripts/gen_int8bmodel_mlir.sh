#!/bin/bash
model_dir=$(dirname $(readlink -f "$0"))

if [ ! $1 ]; then
    target=bm1684x
    target_dir=BM1684X
else
    target=${1,,}
    target_dir=${target^^}
fi

outdir=../models/$target_dir

gen_mlir()
{
    model_transform.py \
        --model_name yolact \
        --model_def ../models/onnx/yolact.onnx \
        --input_shapes [[$1,3,550,550]] \
        --mean 103.94,116.17,123.68 \
        --scale 0.01742767514,0.01750393838,0.01712328767 \
        --keep_aspect_ratio \
        --pixel_format rgb \
        --mlir yolact_$1b.mlir
}

function gen_cali_table()
{
    run_calibration.py yolact_$1b.mlir \
        --dataset ../datasets/coco128/ \
        --input_num 128 \
        -o yolact_cali_table
}

gen_int8bmodel()
{
    model_deploy.py \
        --mlir yolact_$1b.mlir \
        --quantize INT8 \
        --chip ${target} \
        --calibration_table yolact_cali_table \
        --model yolact_${target}_int8_$1b.bmodel \
        --disable_layer_group

    mv yolact_${target}_int8_$1b.bmodel $outdir/
}

pushd $model_dir
if [ ! -d $outdir ]; then
    mkdir -p $outdir
fi

# batch_size=1
gen_mlir 1
gen_cali_table 1
gen_int8bmodel 1

# batch_size=4
gen_mlir 4
gen_int8bmodel 4

popd
