#===----------------------------------------------------------------------===#
#
# Copyright (C) 2022 Sophgo Technologies Inc.  All rights reserved.
#
# SOPHON-DEMO is licensed under the 2-Clause BSD License except for the
# third-party components.
#
#===----------------------------------------------------------------------===#
import cv2
import os
import time
import json
import argparse
import numpy as np
import sophon.sail as sail
from postprocess_numpy import PostProcess
from utils import COLORS
import logging
logging.basicConfig(level=logging.INFO)
# sail.set_print_flag(1)

class CenterNet:
    def __init__(self, args):
        # load bmodel
        self.net = sail.Engine(args.bmodel, args.dev_id, sail.IOMode.SYSO)
        logging.debug("load {} success!".format(args.bmodel))
        self.handle = self.net.get_handle()
        self.bmcv = sail.Bmcv(self.handle)
        self.graph_name = self.net.get_graph_names()[0]
        
        # get input
        self.input_name = self.net.get_input_names(self.graph_name)[0]
        self.input_dtype= self.net.get_input_dtype(self.graph_name, self.input_name)
        self.img_dtype = self.bmcv.get_bm_image_data_format(self.input_dtype)
        self.input_scale = self.net.get_input_scale(self.graph_name, self.input_name)
        self.input_shape = self.net.get_input_shape(self.graph_name, self.input_name)
        self.input_shapes = {self.input_name: self.input_shape}
        
        # get output
        self.output_names = self.net.get_output_names(self.graph_name)
        self.output_tensors = {}
        self.output_scales = {}
        for output_name in self.output_names:
            output_shape = self.net.get_output_shape(self.graph_name, output_name)
            output_dtype = self.net.get_output_dtype(self.graph_name, output_name)
            output_scale = self.net.get_output_scale(self.graph_name, output_name)
            output = sail.Tensor(self.handle, output_shape, output_dtype, True, True)
            self.output_tensors[output_name] = output
            self.output_scales[output_name] = output_scale
        
        # check batch size 
        self.batch_size = self.input_shape[0]
        suppoort_batch_size = [1, 2, 3, 4, 8, 16, 32, 64, 128, 256]
        if self.batch_size not in suppoort_batch_size:
            raise ValueError('batch_size must be {} for bmcv, but got {}'.format(suppoort_batch_size, self.batch_size))
        self.net_h = self.input_shape[2]
        self.net_w = self.input_shape[3]
        
        # init preprocess
        self.use_resize_padding = True
        self.use_vpp = False
        mean = np.array([0.408, 0.447, 0.470], dtype=np.float32)
        std = np.array([0.289, 0.274, 0.278], dtype=np.float32)
        a_list = 1 / 255 / std
        b_list = -mean / std
        self.ab = []
        for i in range(3):
            self.ab.append(self.input_scale * a_list[i])
            self.ab.append(self.input_scale * b_list[i])
        
        # init postprocess
        self.conf_thresh = args.conf_thresh
        self.agnostic = False
        self.multi_label = True
        self.max_det = 1000
        self.postprocess = PostProcess(
            conf_thresh=self.conf_thresh,
            
        )
        
        # init time
        self.preprocess_time = 0.0
        self.inference_time = 0.0
        self.postprocess_time = 0.0

    def init(self):
        self.preprocess_time = 0.0
        self.inference_time = 0.0
        self.postprocess_time = 0.0
        
    def preprocess_bmcv(self, input_bmimg):
        rgb_planar_img = sail.BMImage(self.handle, input_bmimg.height(), input_bmimg.width(),
                                          sail.Format.FORMAT_RGB_PLANAR, sail.DATA_TYPE_EXT_1N_BYTE)
        self.bmcv.convert_format(input_bmimg, rgb_planar_img)
        resized_img_rgb, ratio, txy = self.resize_bmcv(rgb_planar_img)
        preprocessed_bmimg = sail.BMImage(self.handle, self.net_h, self.net_w, sail.Format.FORMAT_RGB_PLANAR, self.img_dtype)
        self.bmcv.convert_to(resized_img_rgb, preprocessed_bmimg, ((self.ab[0], self.ab[1]), \
                                                                 (self.ab[2], self.ab[3]), \
                                                                 (self.ab[4], self.ab[5])))
        return preprocessed_bmimg, ratio, txy

    def resize_bmcv(self, bmimg):
        """
        resize for single sail.BMImage
        :param bmimg:
        :return: a resize image of sail.BMImage
        """
        img_w = bmimg.width()
        img_h = bmimg.height()
        if self.use_resize_padding:
            r_w = self.net_w / img_w
            r_h = self.net_h / img_h
            if r_h > r_w:
                tw = self.net_w
                th = int(r_w * img_h)
                tx1 = tx2 = 0
                ty1 = int((self.net_h - th) / 2)
                ty2 = self.net_h - th - ty1
            else:
                tw = int(r_h * img_w)
                th = self.net_h
                tx1 = int((self.net_w - tw) / 2)
                tx2 = self.net_w - tw - tx1
                ty1 = ty2 = 0

            ratio = (min(r_w, r_h), min(r_w, r_h))
            txy = (tx1, ty1)
            attr = sail.PaddingAtrr()
            attr.set_stx(tx1)
            attr.set_sty(ty1)
            attr.set_w(tw)
            attr.set_h(th)
            attr.set_r(0)
            attr.set_g(0)
            attr.set_b(0)
            
            preprocess_fn = self.bmcv.vpp_crop_and_resize_padding if self.use_vpp else self.bmcv.crop_and_resize_padding
            resized_img_rgb = preprocess_fn(bmimg, 0, 0, img_w, img_h, self.net_w, self.net_h, attr)
        else:
            r_w = self.net_w / img_w
            r_h = self.net_h / img_h
            ratio = (r_w, r_h)
            txy = (0, 0)
            preprocess_fn = self.bmcv.vpp_resize if self.use_vpp else self.bmcv.resize
            resized_img_rgb = preprocess_fn(bmimg, self.net_w, self.net_h)
        return resized_img_rgb, ratio, txy
    
    def predict(self, input_tensor, img_num):
        """
        ensure output order: loc_data, conf_preds, mask_data, proto_data
        Args:
            input_tensor:
        Returns:
        """
        input_tensors = {self.input_name: input_tensor} 
        self.net.process(self.graph_name, input_tensors, self.input_shapes, self.output_tensors)
        outputs_dict = {}
        for name in self.output_names:
            # outputs_dict[name] = self.output_tensors[name].asnumpy()[:img_num] * self.output_scales[name]
            outputs_dict[name] = self.output_tensors[name].asnumpy()[:img_num]
        # resort
        out_keys = list(outputs_dict.keys())
        ord = []
        for n in self.output_names:
            for i, k in enumerate(out_keys):
                if n in k:
                    ord.append(i)
                    break
        out = [outputs_dict[out_keys[i]] for i in ord]
        return out

    def __call__(self, bmimg_list):
        img_num = len(bmimg_list)
        ori_size_list = []
        ratio_list = []
        txy_list = []
        if self.batch_size == 1:
            ori_h, ori_w =  bmimg_list[0].height(), bmimg_list[0].width()
            ori_size_list.append((ori_w, ori_h))
            start_time = time.time()      
            preprocessed_bmimg, ratio, txy = self.preprocess_bmcv(bmimg_list[0])
            self.preprocess_time += time.time() - start_time
            ratio_list.append(ratio)
            txy_list.append(txy)
            
            input_tensor = sail.Tensor(self.handle, self.input_shape, self.input_dtype,  False, False)
            self.bmcv.bm_image_to_tensor(preprocessed_bmimg, input_tensor)
                
        else:
            BMImageArray = eval('sail.BMImageArray{}D'.format(self.batch_size))
            bmimgs = BMImageArray()
            for i in range(img_num):
                ori_h, ori_w =  bmimg_list[i].height(), bmimg_list[i].width()
                ori_size_list.append((ori_w, ori_h))
                start_time = time.time()
                preprocessed_bmimg, ratio, txy  = self.preprocess_bmcv(bmimg_list[i])
                self.preprocess_time += time.time() - start_time
                ratio_list.append(ratio)
                txy_list.append(txy)
                bmimgs[i] = preprocessed_bmimg.data()
            input_tensor = sail.Tensor(self.handle, self.input_shape, self.input_dtype,  False, False)
            self.bmcv.bm_image_to_tensor(bmimgs, input_tensor)
            
        start_time = time.time()
        outputs = self.predict(input_tensor, img_num)[0]
        self.inference_time += time.time() - start_time
        
        start_time = time.time()
        results = self.postprocess(outputs, ori_size_list, ratio_list, txy_list)
        self.postprocess_time += time.time() - start_time

        return results

def draw_bmcv(bmcv, image, boxes, masks=None, classes_ids=None, conf_scores=None, conf_thresh=0.2):
    thickness = 2
    for idx in range(len(boxes)):
        if conf_scores[idx] < conf_thresh:
            continue
        x1, y1, x2, y2 = boxes[idx, :].astype(np.int32).tolist()
        if classes_ids is not None:
            color = np.array(COLORS[int(classes_ids[idx]) + 1]).astype(np.uint8).tolist()
        else:
            color = (0, 0, 255)    
        if (x2 - x1) <= thickness * 2 or (y2 - y1) <= thickness * 2:
            logging.info("width or height too small, this rect will not be drawed: (x1={},y1={},w={},h={})".format(x1, y1, x2-x1, y2-y1))
        else:
            bmcv.rectangle(image, x1, y1, (x2 - x1), (y2 - y1), color, 2)        
        logging.debug("class id={}, score={}, (x1={},y1={},w={},h={})".format(int(classes_ids[idx]), conf_scores[idx], x1, y1, x2-x1, y2-y1))

def main(opt):
    # check params
    if not os.path.exists(args.input):
        raise FileNotFoundError('{} is not existed.'.format(args.input))
    if not os.path.exists(args.bmodel):
        raise FileNotFoundError('{} is not existed.'.format(args.bmodel))
    
    # creat save path
    output_dir = "./results"
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)
    output_img_dir = os.path.join(output_dir, 'images')
    if not os.path.exists(output_img_dir):
        os.mkdir(output_img_dir)

    # initialize net
    centernet = CenterNet(args)
    batch_size = centernet.batch_size
    
    # # warm up 
    # bmimg = sail.BMImage(centernet.handle, 1080, 1920, sail.Format.FORMAT_YUV420P, sail.DATA_TYPE_EXT_1N_BYTE)
    # for i in range(10):
    #     results = centernet([bmimg])
    # centernet.init()

    decode_time = 0.0
    # test images
    # if os.path.isdir(args.input):     
    bmimg_list = []
    filename_list = []
    results_list = []
    cn = 0
    for root, dirs, filenames in os.walk(args.input):
        for filename in filenames:
            if os.path.splitext(filename)[-1].lower() not in ['.jpg','.png','.jpeg','.bmp','.webp']:
                continue
            img_file = os.path.join(root, filename)
            cn += 1
            logging.info("{}, img_file: {}".format(cn, img_file))
            # decode
            start_time = time.time()
            decoder = sail.Decoder(img_file, False, args.dev_id)
            bmimg = sail.BMImage()
            ret = decoder.read(centernet.handle, bmimg)    
            # print(bmimg.format(), bmimg.dtype())
            if ret != 0:
                logging.error("{} decode failure.".format(img_file))
                continue
            decode_time += time.time() - start_time
            # cv2.imwrite('debug/bmimg_%d.jpg'%cn, bmimg.asmat())
            bmimg_list.append(bmimg)
            filename_list.append(filename)
            if len(bmimg_list) == batch_size:
                # for i in range(4):
                #     cv2.imwrite('debug/bmimg_list_%d.jpg'%i, bmimg_list[i].asmat())
                # predict
                results = centernet(bmimg_list)

                for i, filename in enumerate(filename_list):
                    det = results[i]
                    # save image
                    img_bgr_planar = centernet.bmcv.convert_format(bmimg_list[i])
                    draw_bmcv(centernet.bmcv, img_bgr_planar, det[:,:4], masks=None, classes_ids=det[:, -1], conf_scores=det[:, -2], conf_thresh=opt.conf_thresh)
                    centernet.bmcv.imwrite(os.path.join(output_img_dir, filename), img_bgr_planar)
                    
                    # save result
                    res_dict = dict()
                    res_dict['image_name'] = filename
                    res_dict['bboxes'] = []
                    for idx in range(det.shape[0]):
                        bbox_dict = dict()
                        x1, y1, x2, y2, score, category_id = det[idx]
                        bbox_dict['bbox'] = [int(x1), int(y1), int(x2 - x1), int(y2 -y1)]
                        bbox_dict['category_id'] = int(category_id)
                        bbox_dict['score'] = float(score)
                        res_dict['bboxes'].append(bbox_dict)
                    results_list.append(res_dict)
                    
                bmimg_list.clear()
                filename_list.clear()
    if len(bmimg_list):
        results = centernet(bmimg_list)
        for i, filename in enumerate(filename_list):
            det = results[i]
            img_bgr_planar = centernet.bmcv.convert_format(bmimg_list[i])
            draw_bmcv(centernet.bmcv, img_bgr_planar, det[:,:4], masks=None, classes_ids=det[:, -1], conf_scores=det[:, -2], conf_thresh=opt.conf_thresh)
            centernet.bmcv.imwrite(os.path.join(output_img_dir, filename), img_bgr_planar)
            res_dict = dict()
            res_dict['image_name'] = filename
            res_dict['bboxes'] = []
            for idx in range(det.shape[0]):
                bbox_dict = dict()
                x1, y1, x2, y2, score, category_id = det[idx]
                bbox_dict['bbox'] = [int(x1), int(y1), int(x2 - x1), int(y2 -y1)]
                bbox_dict['category_id'] = int(category_id)
                bbox_dict['score'] = float(score)
                res_dict['bboxes'].append(bbox_dict)
            results_list.append(res_dict)
        bmimg_list.clear()
        filename_list.clear()
        
    # save results
    if args.input[-1] == '/':
        args.input = args.input[:-1]
    json_name = os.path.split(args.bmodel)[-1] + "_" + os.path.split(args.input)[-1] + "_bmcv" + "_python_result.json"
    with open(os.path.join(output_dir, json_name), 'w') as jf:
        # json.dump(results_list, jf)
        json.dump(results_list, jf, indent=4, ensure_ascii=False)
    logging.info("result saved in {}".format(os.path.join(output_dir, json_name)))
    
    
    # calculate speed  
    logging.info("------------------ Predict Time Info ----------------------")
    decode_time = decode_time / cn
    preprocess_time = centernet.preprocess_time / cn
    inference_time = centernet.inference_time / cn
    postprocess_time = centernet.postprocess_time / cn
    logging.info("decode_time(ms): {:.2f}".format(decode_time * 1000))
    logging.info("preprocess_time(ms): {:.2f}".format(preprocess_time * 1000))
    logging.info("inference_time(ms): {:.2f}".format(inference_time * 1000))
    logging.info("postprocess_time(ms): {:.2f}".format(postprocess_time * 1000))
    # average_latency = decode_time + preprocess_time + inference_time + postprocess_time
    # qps = 1 / average_latency
    # logging.info("average latency time(ms): {:.2f}, QPS: {:2f}".format(average_latency * 1000, qps))              

def argsparser():
    parser = argparse.ArgumentParser(prog=__file__)
    parser.add_argument('--input', type=str, default='../datasets/test', help='path of input')
    parser.add_argument('--bmodel', type=str, default='../models/BM1684/centernet_fp32_1b.bmodel', help='path of bmodel')
    parser.add_argument('--dev_id', type=int, default=0, help='dev id')
    parser.add_argument('--conf_thresh', type=float, default=0.35, help='confidence threshold')
    args = parser.parse_args()
    return args

if __name__ == "__main__":
    args = argsparser()
    main(args)
    print('all done.')








