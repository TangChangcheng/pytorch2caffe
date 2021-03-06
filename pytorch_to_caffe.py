

import traceback

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from torch.autograd import Variable
from torch.nn.modules.utils import _pair

from .Caffe import caffe_net, layer_param


"""
How to support a new layer type:
 layer_name=log.add_layer(layer_type_name)
 top_blobs=log.add_blobs(<output of that layer>)
 layer=caffe_net.Layer_param(xxx)
 <set layer parameters>
 [<layer.add_data(*datas)>]
 log.cnet.add_layer(layer)
 
Please MUTE the inplace operations to avoid not find in graph
"""

# TODO: support the inplace output of the layers

class Blob_LOG():
    def __init__(self):
        self.data={}
    def __setitem__(self, key, value):
        self.data[key]=value
    def __getitem__(self, key):
        return self.data[key]
    def __len__(self):
        return len(self.data)

NET_INITTED=False

# 转换原理解析：通过记录
class TransLog(object):
    def __init__(self):
        """
        doing init() with inputs Variable before using it
        """
        self.layers={}
        self.detail_layers={}  
        self.detail_blobs={}  
        self._blobs=Blob_LOG()
        self._blobs_data=[]
        self.cnet=caffe_net.Caffemodel('')
        self.debug=True
        

    def init(self,inputs, _id):
        """
        :param inputs: is a list of input variables
        """
        self.add_blobs(inputs)
        self._id = _id

    def add_layer(self, name='layer', torch_name=None):
        if name in self.layers:
            return self.layers[name]
        if name not in self.detail_layers.keys():
            self.detail_layers[name] =0
        self.detail_layers[name] +=1
        name='{}_{}_{}'.format(self._id, name,self.detail_layers[name])
        self.layers[name]=name
        if self.debug:
            print("{} was added to layers".format(self.layers[name]))
        torch_to_caffe_names[torch_name] = self.layers[name]
        return self.layers[name]

    def add_blobs(self, blobs, name='blob',with_num=True):
        rst=[]
        for blob in blobs:
            self._blobs_data.append(blob) # to block the memory address be rewrited
            blob_id=int(id(blob))
            if name not in self.detail_blobs.keys():
                self.detail_blobs[name] =0
            self.detail_blobs[name] +=1           
            if with_num:
                rst.append('{}{}'.format(name,self.detail_blobs[name]))
            else:
                rst.append('{}'.format(name))
            if self.debug:
                print("{}:{} was added to blobs".format(blob_id,rst[-1]))
            self._blobs[blob_id]=rst[-1]
        return rst
        
    def blobs(self, var):
        _var=id(var)
        try:
            if self.debug:
                print("{}:{} getting".format(_var, self._blobs[_var]))
            return self._blobs[_var]
        except:
            print("WARNING: CANNOT FOUND blob {}".format(_var))
            return 'None'

log=TransLog()

layer_names={}
torch_to_caffe_names = {}
def _conv2d(raw,input, weight, bias=None, stride=1, padding=0, dilation=1, groups=1, torch_name=None):
    x=raw(input,weight,bias,stride,padding,dilation,groups)
    name=log.add_layer(name='conv', torch_name=torch_name)
    log.add_blobs([x],name='conv_blob')
    layer=caffe_net.Layer_param(name=name, type='Convolution',
                                bottom=[log.blobs(input)], top=[log.blobs(x)])
    layer.conv_param(x.size()[1],weight.size()[2:],stride=_pair(stride),
                     pad=_pair(padding),dilation=_pair(dilation),bias_term=bias is not None,groups=groups)
    if bias is not None:
        layer.add_data(weight.cpu().data.numpy(),bias.cpu().data.numpy())
    else:
        layer.param.convolution_param.bias_term=False
        layer.add_data(weight.cpu().data.numpy())
    log.cnet.add_layer(layer)
    return x

def _conv_transpose2d(raw,input, weight, bias=None, stride=1, padding=0, output_padding=0, groups=1, dilation=1,torch_name=None):
    x=raw(input, weight, bias, stride, padding, output_padding, groups, dilation)
    name=log.add_layer(name='conv_transpose', torch_name=torch_name)
    log.add_blobs([x],name='conv_transpose_blob')
    layer=caffe_net.Layer_param(name=name, type='Deconvolution',
                                bottom=[log.blobs(input)], top=[log.blobs(x)])
    layer.conv_param(x.size()[1],weight.size()[2:],stride=_pair(stride),
                     pad=_pair(padding),dilation=_pair(dilation),bias_term=bias is not None, groups = groups)
    if bias is not None:
        layer.add_data(weight.cpu().data.numpy(),bias.cpu().data.numpy())
    else:
        layer.param.convolution_param.bias_term=False
        layer.add_data(weight.cpu().data.numpy())
    log.cnet.add_layer(layer)
    return x

def _linear(raw,input, weight, bias=None,torch_name=None):
    x=raw(input,weight,bias)
    layer_name=log.add_layer(name='fc', torch_name=torch_name)
    top_blobs=log.add_blobs([x],name='fc_blob')
    layer=caffe_net.Layer_param(name=layer_name,type='InnerProduct',
                                bottom=[log.blobs(input)],top=[log.blobs(x)])
    layer.fc_param(x.size()[-1],has_bias=bias is not None)
    if bias is not None:
        layer.add_data(weight.cpu().data.numpy(),bias.cpu().data.numpy())
    else:
        layer.add_data(weight.cpu().data.numpy())
    log.cnet.add_layer(layer)
    return x

def _split(raw,tensor, split_size, dim=0,torch_name=None):
    # split in pytorch is slice in caffe
    x=raw(tensor, split_size, dim)
    layer_name=log.add_layer('split', torch_name=torch_name)
    top_blobs=log.add_blobs(x,name='split_blob')
    layer=caffe_net.Layer_param(name=layer_name, type='Slice',
                                bottom=[log.blobs(tensor)], top=[log.blobs(x)])
    if not isinstance(split_size, (list, tuple)):
        # int, split size
        slice_num=int(np.floor(tensor.size()[dim]/split_size))
        slice_param=caffe_net.pb.SliceParameter(axis=dim,slice_point=[split_size*i for i in range(1,slice_num)])
    else:
        # split sections
        for i in range(1, len(split_size)):
            split_size[i] += split_size[i-1]
        slice_param=caffe_net.pb.SliceParameter(axis=dim, slice_point=split_size[:-1])
    layer.param.slice_param.CopyFrom(slice_param)
    log.cnet.add_layer(layer)
    return x


def _pool(type,raw,input,x,kernel_size,stride,padding,ceil_mode,torch_name=None):
    # TODO dilation,ceil_mode,return indices
    layer_name = log.add_layer(name='{}_pool'.format(type), torch_name=torch_name)
    top_blobs = log.add_blobs([x], name='{}_pool_blob'.format(type))
    layer = caffe_net.Layer_param(name=layer_name, type='Pooling',
                                  bottom=[log.blobs(input)], top=[log.blobs(x)])
    # TODO w,h different kernel, stride and padding
    # processing ceil mode
    layer.pool_param(kernel_size=kernel_size, stride=kernel_size if stride is None else stride,
                     pad=padding, type=type.upper() , ceil_mode = ceil_mode)
    log.cnet.add_layer(layer)
    if ceil_mode==False and stride is not None:
        oheight = (input.size()[2] - _pair(kernel_size)[0] + 2 * _pair(padding)[0]) % (_pair(stride)[0])
        owidth = (input.size()[3] - _pair(kernel_size)[1] + 2 * _pair(padding)[1]) % (_pair(stride)[1])
        if oheight!=0 or owidth!=0:
            caffe_out=raw(input, kernel_size, stride, padding, ceil_mode=True)
            print("WARNING: the output shape miss match at {}: "
            
                  "input {} output---Pytorch:{}---Caffe:{}\n"
                  "This is caused by the different implementation that ceil mode in caffe and the floor mode in pytorch.\n"
                  "You can add the clip layer in caffe prototxt manually if shape mismatch error is caused in caffe. ".format(layer_name,input.size(),x.size(),caffe_out.size()))

def _max_pool2d(raw,input, kernel_size, stride=None, padding=0, dilation=1,
               ceil_mode=False, return_indices=False,torch_name=None):
    x = raw(input, kernel_size, stride, padding, dilation,ceil_mode, return_indices)
    _pool('max',raw,input, x, kernel_size, stride, padding,ceil_mode,torch_name=torch_name)
    return x

def _avg_pool2d(raw,input, kernel_size, stride = None, padding = 0, ceil_mode = False, count_include_pad = True, divisor_override=None,torch_name=None):
    x = raw(input, kernel_size, stride, padding, ceil_mode, count_include_pad, divisor_override)
    _pool('ave',raw,input, x, kernel_size, stride, padding,ceil_mode,torch_name=torch_name)
    return x

def _max(raw,*args,torch_name=None):
    x=raw(*args)
    if len(args)==1:
        # TODO max in one tensor
        assert NotImplementedError
    else:
        bottom_blobs=[]
        for arg in args:
            bottom_blobs.append(log.blobs(arg))
        layer_name=log.add_layer(name='max', torch_name=torch_name)
        top_blobs=log.add_blobs([x],name='max_blob')
        layer=caffe_net.Layer_param(name=layer_name,type='Eltwise',
                                    bottom=bottom_blobs,top=[log.blobs(x)])
        layer.param.eltwise_param.operation =2
        log.cnet.add_layer(layer)
    return x

def _cat(raw,inputs, dimension=0,torch_name=None):
    x=raw(inputs, dimension)
    bottom_blobs=[]
    for input in inputs:
        bottom_blobs.append(log.blobs(input))
    layer_name=log.add_layer(name='cat', torch_name=torch_name)
    top_blobs=log.add_blobs([x],name='cat_blob')
    layer=caffe_net.Layer_param(name=layer_name,type='Concat',
                                bottom=bottom_blobs,top=[log.blobs(x)])
    layer.param.concat_param.axis =dimension
    log.cnet.add_layer(layer)
    return x

def _dropout(raw,input,p=0.5, training=False, inplace=False,torch_name=None):
    bottom_blobs=[log.blobs(input)]
    x=raw(input,p, training, False)
    layer_name=log.add_layer(name='dropout', torch_name=torch_name)
    top_blobs=log.add_blobs([x],name=bottom_blobs[0],with_num=False)
    layer=caffe_net.Layer_param(name=layer_name,type='Dropout',
                                bottom=bottom_blobs,top=[log.blobs(x)])
    layer.param.dropout_param.dropout_ratio = p
    layer.param.include.extend([caffe_net.pb.NetStateRule(phase=0)]) # 1 for test, 0 for train
    log.cnet.add_layer(layer)
    return x

def _threshold(raw,input, threshold, value, inplace=False,torch_name=None):
    # for threshold or relu
    if threshold==0 and value==0:
        x = raw(input,threshold, value, inplace)
        bottom_blobs=[log.blobs(input)]
        name = log.add_layer(name='relu', torch_name=torch_name)
        log.add_blobs([x], name='relu_blob')
        layer = caffe_net.Layer_param(name=name, type='ReLU',
                                      bottom=bottom_blobs, top=[log.blobs(x)])
        log.cnet.add_layer(layer)
        return x
    if value!=0:
        raise NotImplemented("value !=0 not implemented in caffe")
    x=raw(input,input, threshold, value, inplace)
    bottom_blobs=[log.blobs(input)]
    layer_name=log.add_layer(name='threshold', torch_name=torch_name)
    top_blobs=log.add_blobs([x],name='threshold_blob')
    layer=caffe_net.Layer_param(name=layer_name,type='Threshold',
                                bottom=bottom_blobs,top=[log.blobs(x)])
    layer.param.threshold_param.threshold = threshold
    log.cnet.add_layer(layer)
    return x

def _relu(raw, input, inplace=False,torch_name=None):
    # for threshold or prelu
    x = raw(input, False)
    name = log.add_layer(name='relu', torch_name=torch_name)
    log.add_blobs([x], name='relu_blob')
    layer = caffe_net.Layer_param(name=name, type='ReLU',
                                  bottom=[log.blobs(input)], top=[log.blobs(x)])
    log.cnet.add_layer(layer)
    return x

def _relu6(raw, input, inplace=False,torch_name=None):
    # FIXME: as dpu do not suppport relu6, try use relu
    x = raw(input, False)
    name = log.add_layer(name='relu', torch_name=torch_name)
    log.add_blobs([x], name='relu_blob')
    layer = caffe_net.Layer_param(name=name, type='ReLU',
                                  bottom=[log.blobs(input)], top=[log.blobs(x)])
    log.cnet.add_layer(layer)
    return x

def _prelu(raw, input, weight,torch_name=None):
    # for threshold or prelu
    x = raw(input, weight)
    bottom_blobs=[log.blobs(input)]
    name = log.add_layer(name='prelu', torch_name=torch_name)
    log.add_blobs([x], name='prelu_blob')
    layer = caffe_net.Layer_param(name=name, type='PReLU',
                                  bottom=bottom_blobs, top=[log.blobs(x)])
    if weight.size()[0]==1:
        layer.param.prelu_param.channel_shared=True
        layer.add_data(weight.cpu().data.numpy()[0])
    else:
        layer.add_data(weight.cpu().data.numpy())
    log.cnet.add_layer(layer)
    return x

def _leaky_relu(raw, input, negative_slope=0.01, inplace=False,torch_name=None):
    x = raw(input, negative_slope)
    name = log.add_layer(name='leaky_relu', torch_name=torch_name)
    log.add_blobs([x], name='leaky_relu_blob')
    layer = caffe_net.Layer_param(name=name, type='ReLU',
                                  bottom=[log.blobs(input)], top=[log.blobs(x)])
    layer.param.relu_param.negative_slope=negative_slope
    log.cnet.add_layer(layer)
    return x

def _tanh(raw, input,torch_name=None):
    # for tanh activation
    x = raw(input)
    name = log.add_layer(name='tanh', torch_name=torch_name)
    log.add_blobs([x], name='tanh_blob')
    layer = caffe_net.Layer_param(name=name, type='TanH',
                                  bottom=[log.blobs(input)], top=[log.blobs(x)])
    log.cnet.add_layer(layer)
    return x

def _softmax(raw, input, dim=None, _stacklevel=3,torch_name=None):
    # for F.softmax
    x=raw(input, dim=dim)
    if dim is None:
        dim=F._get_softmax_dim('softmax', input.dim(), _stacklevel)
    bottom_blobs=[log.blobs(input)]
    name = log.add_layer(name='softmax', torch_name=torch_name)
    log.add_blobs([x], name='softmax_blob')
    layer = caffe_net.Layer_param(name=name, type='Softmax',
                                  bottom=bottom_blobs, top=[log.blobs(x)])
    layer.param.softmax_param.axis=dim
    log.cnet.add_layer(layer)
    return x

def _batch_norm(raw,input, running_mean, running_var, weight=None, bias=None,
               training=False, momentum=0.1, eps=1e-5,torch_name=None):
    # because the runing_mean and runing_var will be changed after the _batch_norm operation, we first save the parameters

    x = raw(input, running_mean, running_var, weight, bias,
               training, momentum, eps)
    bottom_blobs = [log.blobs(input)]
    layer_name1 = log.add_layer(name='batch_norm', torch_name=torch_name)
    top_blobs = log.add_blobs([x], name='batch_norm_blob')
    layer1 = caffe_net.Layer_param(name=layer_name1, type='BatchNorm',
                                   bottom=bottom_blobs, top=[log.blobs(x)])
    if running_mean is None or running_var is None:
        # not use global_stats, normalization is performed over the current mini-batch
        layer1.batch_norm_param(use_global_stats=0,eps=eps)
    else:
        # layer1.batch_norm_param(use_global_stats=1, eps=eps)
        layer1.batch_norm_param(eps=eps)
        running_mean_clone = running_mean.clone()
        running_var_clone = running_var.clone()
        # if weight is not None and bias is not None:
        #     layer1.add_data(running_mean_clone.cpu().numpy(), running_var_clone.cpu().numpy(), weight.cpu().data.numpy(), bias.cpu().data.numpy())
        # else:
        layer1.add_data(running_mean_clone.cpu().numpy(), running_var_clone.cpu().numpy(), np.array([1.0]))
    log.cnet.add_layer(layer1)

    if weight is not None and bias is not None:
        layer_name2 = log.add_layer(name='bn_scale', torch_name=torch_name)
        layer2 = caffe_net.Layer_param(name=layer_name2, type='Scale',
                                       bottom=top_blobs, top=[log.blobs(x)])
        layer2.param.scale_param.bias_term = True
        layer2.add_data(weight.cpu().data.numpy(), bias.cpu().data.numpy())
        log.cnet.add_layer(layer2)

    return x

def _instance_norm(raw, input, running_mean=None, running_var=None, weight=None,
                  bias=None, use_input_stats=True, momentum=0.1, eps=1e-5,torch_name=None):
    # TODO: the batch size!=1 view operations
    print("WARNING: The Instance Normalization transfers to Caffe using BatchNorm, so the batch size should be 1")
    if running_var is not None or weight is not None:
        # TODO: the affine=True or track_running_stats=True case
        raise NotImplementedError("not implement the affine=True or track_running_stats=True case InstanceNorm")
    x= torch.batch_norm(
        input, weight, bias, running_mean, running_var,
        use_input_stats, momentum, eps,torch.backends.cudnn.enabled)
    bottom_blobs = [log.blobs(input)]
    layer_name1 = log.add_layer(name='instance_norm', torch_name=torch_name)
    top_blobs = log.add_blobs([x], name='instance_norm_blob')
    layer1 = caffe_net.Layer_param(name=layer_name1, type='BatchNorm',
                                   bottom=bottom_blobs, top=[log.blobs(x)])
    if running_mean is None or running_var is None:
        # not use global_stats, normalization is performed over the current mini-batch
        layer1.batch_norm_param(use_global_stats=0,eps=eps)
        running_mean=torch.zeros(input.size()[1])
        running_var=torch.ones(input.size()[1])
    else:
        layer1.batch_norm_param(use_global_stats=1, eps=eps)
    running_mean_clone = running_mean.clone()
    running_var_clone = running_var.clone()
    layer1.add_data(running_mean_clone.cpu().numpy(), running_var_clone.cpu().numpy(), np.array([1.0]))
    log.cnet.add_layer(layer1)
    if weight is not None and bias is not None:
        layer_name2 = log.add_layer(name='bn_scale', torch_name=torch_name)
        layer2 = caffe_net.Layer_param(name=layer_name2, type='Scale',
                                       bottom=top_blobs, top=[log.blobs(x)])
        layer2.param.scale_param.bias_term = True
        layer2.add_data(weight.cpu().data.numpy(), bias.cpu().data.numpy())
        log.cnet.add_layer(layer2)
    return x


#upsample layer
def _interpolate(raw, input,size=None, scale_factor=None, mode='nearest', align_corners=None,torch_name=None):
    # 定义的参数包括 scale,即输出与输入的尺寸比例,如 2;scale_h、scale_w,
    # 同 scale,分别为 h、w 方向上的尺寸比例;pad_out_h、pad_out_w,仅在 scale 为 2 时
    # 有用,对输出进行额外 padding 在 h、w 方向上的数值;upsample_h、upsample_w,输
    # 出图像尺寸的数值。在 Upsample 的相关代码中,推荐仅仅使用 upsample_h、
    # upsample_w 准确定义 Upsample 层的输出尺寸,其他所有的参数都不推荐继续使用。
    # for nearest _interpolate
    if mode != "nearest" or align_corners != None:
        raise NotImplementedError("not implement F.interpolate totoaly")
    x = raw(input,size , scale_factor ,mode)

    layer_name = log.add_layer(name='upsample', torch_name=torch_name)
    top_blobs = log.add_blobs([x], name='upsample_blob'.format(type))
    layer = caffe_net.Layer_param(name=layer_name, type='Upsample',
                                  bottom=[log.blobs(input)], top=[log.blobs(x)])

    layer.upsample_param(size =(input.size(2),input.size(3)), scale_factor= scale_factor)
    log.cnet.add_layer(layer)
    return x


#sigmid layer
def _sigmoid(raw, input,torch_name=None):
    # Applies the element-wise function:
    # 
    # Sigmoid(x)= 1/(1+exp(−x)）
    # 
    # ​	
    x = raw(input)
    name = log.add_layer(name='sigmoid', torch_name=torch_name)
    log.add_blobs([x], name='sigmoid_blob')
    layer = caffe_net.Layer_param(name=name, type='Sigmoid',
                                  bottom=[log.blobs(input)], top=[log.blobs(x)])
    log.cnet.add_layer(layer)
    return x

#tanh layer
def _tanh(raw, input,torch_name=None):
    # Applies the element-wise function:
    # 
    # torch.nn.Tanh
    # 
    # ​	
    x = raw(input)
    name = log.add_layer(name='tanh', torch_name=torch_name)
    log.add_blobs([x], name='tanh_blob')
    layer = caffe_net.Layer_param(name=name, type='TanH',
                                  bottom=[log.blobs(input)], top=[log.blobs(x)])
    log.cnet.add_layer(layer)
    return x


def _squeeze(raw, inputs, *args,torch_name=None): 
    x=raw(inputs, *args)
    if not NET_INITTED:
        return x
    layer_name=log.add_layer(name='squeeze', torch_name=torch_name)
    top_blobs=log.add_blobs([x],name='view_blob')
    layer=caffe_net.Layer_param(name=layer_name,type='Reshape',
                                bottom=[log.blobs(inputs)],top=[log.blobs(x)])
    dims = [0, -1]
    layer.param.reshape_param.shape.CopyFrom(caffe_net.pb.BlobShape(dim=dims))

    log.cnet.add_layer(layer)
    return x

def _flatten(raw, inputs, *args,torch_name=None):
    dims = inputs.shape
    x=raw(inputs, *args)
    if len(args) == 0:
        start = 0
    else:
        start = args[0]
    if not NET_INITTED:
        return x
    layer_name=log.add_layer(name='flatten', torch_name=torch_name)
    top_blobs=log.add_blobs([x],name='view_blob')
    layer=caffe_net.Layer_param(name=layer_name,type='Reshape',
                                bottom=[log.blobs(inputs)],top=[log.blobs(x)])
    dims = [dims[i] for i in range(start)] + [-1]
    layer.param.reshape_param.shape.CopyFrom(caffe_net.pb.BlobShape(dim=dims))

    log.cnet.add_layer(layer)
    return x

# ----- for Variable operations --------

def _view(input, *args,torch_name=None):
    x=raw_view(input, *args)
    if not NET_INITTED:
        return x
    layer_name=log.add_layer(name='view', torch_name=torch_name)
    top_blobs=log.add_blobs([x],name='view_blob')
    layer=caffe_net.Layer_param(name=layer_name,type='Reshape',
                                bottom=[log.blobs(input)],top=[log.blobs(x)])
    # TODO: reshpae added to nn_tools layer
    dims=list(args)
    dims[0]=0 # the first dim should be batch_size
    layer.param.reshape_param.shape.CopyFrom(caffe_net.pb.BlobShape(dim=dims))
    log.cnet.add_layer(layer)
    return x


def _mean(input, *args,torch_name=None, **kwargs):
    x=raw_mean(input, *args,**kwargs)
    if not NET_INITTED:
        return x
    layer_name=log.add_layer(name='mean', torch_name=torch_name)
    top_blobs=log.add_blobs([x],name='mean_blob')
    layer=caffe_net.Layer_param(name=layer_name,type='Reduction',
                                bottom=[log.blobs(input)],top=[log.blobs(x)])
    if len(args)==1:
        dim=args[0]
    elif 'dim' in kwargs:
        dim=kwargs['dim']
    else:
        raise NotImplementedError('mean operation must specify a dim')
    layer.param.reduction_param.operation=4
    layer.param.reduction_param.axis=dim
    log.cnet.add_layer(layer)
    return x

def _add(input, *args,torch_name=None):
    x = raw__add__(input, *args)
    if not NET_INITTED:
        return x
    layer_name = log.add_layer(name='add', torch_name=torch_name)
    top_blobs = log.add_blobs([x], name='add_blob')
    if isinstance(args[0], (int, float)):
        # handle add constant bias
        layer = caffe_net.Layer_param(name=layer_name, type='Bias', bottom=[log.blobs(input)], top=[log.blobs(x)])
        layer.bias_param(args[0], trainable=False)
    else:
        # elementwise add
        layer = caffe_net.Layer_param(name=layer_name, type='Eltwise',
                                      bottom=[log.blobs(input),log.blobs(args[0])], top=[log.blobs(x)])
        layer.param.eltwise_param.operation = 1 # sum is 1
    log.cnet.add_layer(layer)
    return x

def _iadd(input, *args,torch_name=None):
    b1, b2 = log.blobs(input),log.blobs(args[0])
    if b1 == 'None' and b2 == 'None':
        return input
    x = raw__iadd__(input, *args)
    if not NET_INITTED:
        return x
    x=x.clone()
    layer_name = log.add_layer(name='iadd', torch_name=torch_name)
    top_blobs = log.add_blobs([x], name='add_blob')
    layer = caffe_net.Layer_param(name=layer_name, type='Eltwise',
                                  bottom=[b1, b2], top=[log.blobs(x)])
    layer.param.eltwise_param.operation = 1 # sum is 1
    log.cnet.add_layer(layer)
    return x

def _sub(input, *args,torch_name=None):
    x = raw__sub__(input, *args)
    if not NET_INITTED:
        return x
    layer_name = log.add_layer(name='sub', torch_name=torch_name)
    top_blobs = log.add_blobs([x], name='sub_blob')
    layer = caffe_net.Layer_param(name=layer_name, type='Eltwise',
                                  bottom=[log.blobs(input),log.blobs(args[0])], top=[log.blobs(x)])
    layer.param.eltwise_param.operation = 1 # sum is 1
    layer.param.eltwise_param.coeff.extend([1.,-1.])
    log.cnet.add_layer(layer)
    return x

def _isub(input, *args):
    x = raw__isub__(input, *args)
    if not NET_INITTED:
        return x
    x=x.clone()
    layer_name = log.add_layer(name='isub', torch_name=torch_name)
    top_blobs = log.add_blobs([x], name='sub_blob')
    layer = caffe_net.Layer_param(name=layer_name, type='Eltwise',
                                  bottom=[log.blobs(input),log.blobs(args[0])], top=[log.blobs(x)])
    layer.param.eltwise_param.operation = 1 # sum is 1
    log.cnet.add_layer(layer)
    return x

# TODO: support scalar operation using power layer (y = (shift + scale * x) ^ power, set shift = 0, power = 1)
def _mul(input, *args,torch_name=None):
    x = raw__mul__(input, *args)
    if not NET_INITTED:
        return x
    # element wise mul using scale layer
    if isinstance(args[0], float):
        layer_name = log.add_layer(name='mul', torch_name=torch_name)
        top_blobs = log.add_blobs([x], name='mul_blob')
        log.add_blobs([args[0]], name='scalar')
        layer = caffe_net.Layer_param(name=layer_name, type='Scale',
                                      bottom=[log.blobs(input), log.blobs(args[0])], top=[log.blobs(x)])
        layer.param.scale_param.bias_term = False
        layer.param.scale_param.axis = 0
        log.cnet.add_layer(layer)
        return x
    assert args[0].shape[0] == input.shape[0] and args[0].shape[1] == input.shape[1]
    if not (args[0].shape[2] == input.shape[2] and args[0].shape[3] == input.shape[3]):
        print("WARNING: DPU cannot handle this implictly-broadcast elementwise multiplication efficiently! {} with {}".format(args[0].shape, input.shape))
        # Handle implicitly broadcast in pytorch, reshape -> scale;
        # Actually this is not support by DPU (2019.10.16)
        # add reshape layer
        assert args[0].shape[2] == 1 and args[0].shape[3] == 1
        layer_name = log.add_layer(name="reshape", torch_name=torch_name)
        y = args[0].view(args[0].shape[0], -1)
        layer_name = log.add_layer(name='mul', torch_name=torch_name)
        top_blobs = log.add_blobs([x], name='mul_blob')
        layer = caffe_net.Layer_param(name=layer_name, type='Scale',
                                      bottom=[log.blobs(input), log.blobs(y)], top=[log.blobs(x)])
        layer.param.scale_param.bias_term = False
        layer.param.scale_param.axis = 0
    else:
        # acutally, dpu only support elementwise...
        layer_name = log.add_layer(name='mul', torch_name=torch_name)
        top_blobs = log.add_blobs([x], name='mul_blob')
        layer = caffe_net.Layer_param(name=layer_name, type='Eltwise',
                                      bottom=[log.blobs(input), log.blobs(args[0])], top=[log.blobs(x)])
        layer.param.eltwise_param.operation = 0  # product is 1
    log.cnet.add_layer(layer)
    return x

def _imul(input, *args,torch_name=None):
    x = raw__imul__(input, *args)
    if not NET_INITTED:
        return x
    x = x.clone()
    layer_name = log.add_layer(name='mul', torch_name=torch_name)
    top_blobs = log.add_blobs([x], name='mul_blob')
    layer = caffe_net.Layer_param(name=layer_name, type='Eltwise',
                                  bottom=[log.blobs(input), log.blobs(args[0])], top=[log.blobs(x)])
    layer.param.eltwise_param.operation = 0  # product is 1
    layer.param.eltwise_param.coeff.extend([1., -1.])
    log.cnet.add_layer(layer)
    return x


# TODO: support division: determine which method is called, now we know that torch.Tensor.__div__ and torch.Tensor.__idiv__ are not called.
def _div(input, *args,torch_name=None):
    x = raw__div__(input, *args)
    if not NET_INITTED:
        return x
    # element wise mul using scale layer
    if isinstance(args[0], float):
        layer_name = log.add_layer(name='div', torch_name=torch_name)
        top_blobs = log.add_blobs([x], name='div_blob')
        layer = caffe_net.Layer_param(name=layer_name, type='Scale',
                                      bottom=[log.blobs(input), log.blobs(args[0])], top=[log.blobs(x)])
        layer.param.scale_param.bias_term = False
        layer.param.scale_param.axis = 0
        log.cnet.add_layer(layer)
        return x

    assert args[0].shape[0] == input.shape[0] and args[0].shape[1] == input.shape[1]
    if not (args[0].shape[2] == input.shape[2] and args[0].shape[3] == input.shape[3]):
        print("WARNING: DPU cannot handle this implictly-broadcast elementwise multiplication efficiently! {} with {}".format(args[0].shape, input.shape))
        # Handle implicitly broadcast in pytorch, reshape -> scale;
        # Actually this is not support by DPU (2019.10.16)
        # add reshape layer
        assert args[0].shape[2] == 1 and args[0].shape[3] == 1
        layer_name = log.add_layer(name="reshape", torch_name=torch_name)
        y = args[0].view(args[0].shape[0], -1)
        layer_name = log.add_layer(name='div', torch_name=torch_name)
        top_blobs = log.add_blobs([x], name='div_blob')
        layer = caffe_net.Layer_param(name=layer_name, type='Scale',
                                      bottom=[log.blobs(input), log.blobs(y)], top=[log.blobs(x)])
        layer.param.scale_param.bias_term = False
        layer.param.scale_param.axis = 0
    else:
        # acutally, dpu only support elementwise...
        layer_name = log.add_layer(name='div', torch_name=torch_name)
        top_blobs = log.add_blobs([x], name='div_blob')
        layer = caffe_net.Layer_param(name=layer_name, type='Eltwise',
                                      bottom=[log.blobs(input), log.blobs(args[0])], top=[log.blobs(x)])
        layer.param.eltwise_param.operation = 0  # product is 1
    log.cnet.add_layer(layer)
    return x

def _idiv(input, *args,torch_name=None):
    x = raw__idiv__(input, *args)
    if not NET_INITTED:
        return x
    x = x.clone()
    layer_name = log.add_layer(name='div', torch_name=torch_name)
    top_blobs = log.add_blobs([x], name='div_blob')
    layer = caffe_net.Layer_param(name=layer_name, type='Eltwise',
                                  bottom=[log.blobs(input), log.blobs(args[0])], top=[log.blobs(x)])
    layer.param.eltwise_param.operation = 0  # product is 1
    layer.param.eltwise_param.coeff.extend([1., -1.])
    log.cnet.add_layer(layer)
    return x


# 核心组件，通过该类，实现对torch的function中的operators的输入，输出以及参数的读取
class Rp(object):
    def __init__(self,raw,replace,**kwargs):
        # replace the raw function to replace function
        self.obj=replace
        self.raw=raw

    def __call__(self,*args,**kwargs):
        if not NET_INITTED:
            return self.raw(*args,**kwargs)
        for stack in traceback.walk_stack(None):
            if 'self' in stack[0].f_locals:
                layer=stack[0].f_locals['self']
                if layer in layer_names:
                    log.pytorch_layer_name=layer_names[layer]
                    print(layer_names[layer])
                    break
        kwargs.update({'torch_name': layer_names[layer]})
        out=self.obj(self.raw, *args, **kwargs)
        # if isinstance(out,Variable):
        #     out=[out]
        return out


F.conv2d=Rp(F.conv2d,_conv2d)
F.linear=Rp(F.linear,_linear)
F.relu=Rp(F.relu,_relu)
F.relu6=Rp(F.relu6,_relu6)
F.leaky_relu=Rp(F.leaky_relu,_leaky_relu)
F.max_pool2d=Rp(F.max_pool2d,_max_pool2d)
F.avg_pool2d=Rp(F.avg_pool2d,_avg_pool2d)
F.dropout=Rp(F.dropout,_dropout)
F.threshold=Rp(F.threshold,_threshold)
F.prelu=Rp(F.prelu,_prelu)
F.batch_norm=Rp(F.batch_norm,_batch_norm)
F.instance_norm=Rp(F.instance_norm,_instance_norm)
F.softmax=Rp(F.softmax,_softmax)
F.conv_transpose2d=Rp(F.conv_transpose2d,_conv_transpose2d)
F.interpolate = Rp(F.interpolate,_interpolate)
F.sigmoid = Rp(F.sigmoid,_sigmoid)
torch.sigmoid = Rp(torch.sigmoid,_sigmoid)
F.tanh = Rp(F.tanh,_tanh)

#add
torch.squeeze = Rp(torch.squeeze, _squeeze)
torch.flatten = Rp(torch.flatten, _flatten)


torch.split=Rp(torch.split,_split)
torch.max=Rp(torch.max,_max)
torch.cat=Rp(torch.cat,_cat)

def _v_sigmoid(tensor):
    return torch.sigmoid(tensor)

for t in [torch.Tensor]:        
    raw_view = t.view
    t.view = _view  
    t.sigmoid = _v_sigmoid
    raw_mean = t.mean
    t.mean = _mean
    raw__add__ = t.__add__
    t.__add__ = _add
    raw__iadd__ = t.__iadd__
    t.__iadd__ = _iadd
    raw__sub__ = t.__sub__
    t.__sub__ = _sub
    raw__isub__ = t.__isub__
    t.__isub__ = _isub
    raw__mul__ = t.__mul__
    t.__mul__=_mul
    raw__imul__ = t.__imul__
    t.__imul__ = _imul
    raw__div__ = t.__div__
    t.__div__ = _div
    raw__idiv__ = t.__idiv__
    t.__idiv__ = _idiv


def trans_net(net,input_var,name='TransferedPytorchModel'):
    print('Starting Transform, This will take a while')
    input_var = input_var.to(net.device)
    log.init([input_var], name)
    log.cnet.net.name=name
    log.cnet.net.input.extend([log.blobs(input_var)])
    log.cnet.net.input_dim.extend(input_var.size())
    global NET_INITTED
    NET_INITTED=True
    for name,layer in net.named_modules():
        layer_names[layer]=name
    print("torch ops name:", layer_names)
    out = net.forward(input_var)
    print('Transform Completed')
    NET_INITTED = False

def save_prototxt(save_name):
    log.cnet.save_prototxt(save_name)

def save_caffemodel(save_name):
    log.cnet.save(save_name)
