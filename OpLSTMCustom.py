import os
from FunctionLoader import make_funloader_code
import theano
import theano.gradient
import theano.tensor as T
import theano.printing
import theano.gof
from theano.sandbox.cuda.basic_ops import (as_cuda_ndarray_variable,
                                           gpu_contiguous)


class LSTMCustomOpGrad(theano.sandbox.cuda.GpuOp):
  __props__ = ("inplace", "fun_name")

  def __init__(self, fun_name):
    super(LSTMCustomOpGrad, self).__init__(self)
    self.inplace = False
    self.fun_name = fun_name

  #TODO: later also accept B
  def make_node(self, Y, H, c, y0, i, Dd, DY, W_re, *custom_inputs):
    c = gpu_contiguous(as_cuda_ndarray_variable(c))
    y0 = gpu_contiguous(as_cuda_ndarray_variable(y0))
    i = gpu_contiguous(as_cuda_ndarray_variable(T.cast(i,'float32')))
    Dd = gpu_contiguous(as_cuda_ndarray_variable(Dd))
    DY = gpu_contiguous(as_cuda_ndarray_variable(DY))
    W_re = gpu_contiguous(as_cuda_ndarray_variable(W_re))
    custom_inputs = [gpu_contiguous(as_cuda_ndarray_variable(x)) for x in custom_inputs]
    assert DY.dtype == 'float32'
    assert Y.dtype == 'float32'
    assert H.dtype == 'float32'
    assert c.dtype == 'float32'
    assert y0.dtype == "float32"
    assert W_re.dtype == "float32"
    for x in custom_inputs:
      assert x.dtype == "float32"
    assert DY.ndim == 3
    assert Y.ndim == 3
    assert H.ndim == 3
    assert c.ndim == 2
    assert y0.ndim == 2
    assert i.ndim == 2
    assert W_re.ndim == 2

    return theano.Apply(self, [Y, H, c, y0, i, Dd, DY, W_re] + custom_inputs, [H.type(), c.type(), y0.type(), W_re.type()])

  def c_support_code(self):
    #do not remove this import as it is used in the c code
    import CustomLSTMFunctions
    crnn_path = os.path.dirname(__file__)
    funloader = make_funloader_code(self.fun_name + "_bwd", 1)
    with open(crnn_path + "/c_support_code_mdlstm.cpp") as f:
      return funloader + f.read()

  def c_code(self, node, name, input_names, output_names, sub):
    Y, H, c, y0, i, Dd, DY, W_re = input_names[:8]
    custom_inputs=input_names[8:]
    custom_inputs_str = ("," if len(custom_inputs) > 0 else "") + ",".join(custom_inputs)
    DZ, Dc, Dy0, DW_re = output_names
    bwd_fun = self.fun_name + "_bwd"
    fail = sub['fail']
    inplace = "true" if self.inplace else "false"
    return """
    //std::cout << "LSTMCustomOpGrad called" << std::endl;
    if(%(DZ)s || %(Dc)s || %(DW_re)s || %(Dy0)s)
    {
      printf("output storage already exists\\n");
      //TODO check if we can reuse it
      Py_XDECREF(%(DZ)s);
      Py_XDECREF(%(Dc)s);
      Py_XDECREF(%(DW_re)s);
      Py_XDECREF(%(Dy0)s);
    }

    CudaNdarray * epsilon = 0;
    CudaNdarray * delta = 0;
    if(%(inplace)s)
    {
      epsilon = %(DY)s;
      delta = %(H)s;
      Py_XINCREF(delta);
    }
    else
    {
      epsilon = (CudaNdarray *) CudaNdarray_Copy(%(DY)s);
      delta = (CudaNdarray *) CudaNdarray_Copy(%(H)s);
    }

    const int * H_dim = CudaNdarray_HOST_DIMS(%(H)s);
    const int * Y_dim = CudaNdarray_HOST_DIMS(%(Y)s);

    int y = 0;
    for(int x = H_dim[0]-1; x >= 0; --x)
    {
      //add recurrent
      bool rightBorder = (x == H_dim[0]-1);
      bool leftBorder = (x == 0);

      //TODO: check if we need to handle boundary case specially
      if(!rightBorder)
      {
        affine_y_x(y, x+1, delta, y, x, %(W_re)s, y, x, epsilon, false, true);
      }

      //call custom function here
      if(!rightBorder)
      {
        CudaNdarray * y_p = 0;
        //x-1?
        PyObject * y_p_obj = PyObject_CallMethod((PyObject*) %(Y)s, "__getitem__", "(i)", x);
        assert(y_p_obj);
        y_p = (CudaNdarray*) y_p_obj;

        PyObject * delta_x_obj = PyObject_CallMethod((PyObject*) delta, "__getitem__", "(i)", x+1);
        assert(delta_x_obj);
        CudaNdarray * delta_x = (CudaNdarray*) delta_x_obj;

        //note: the missing comma is intentionally for the case where there are 0 custom inputs
        std::vector<PyObject*> res_vec = %(bwd_fun)s(y_p, delta_x %(custom_inputs_str)s);
        assert(res_vec.size() == 1);
        Py_XDECREF(delta_x);
        CudaNdarray * Dy_p = (CudaNdarray*) res_vec[0];

        //copy to epsilon
        float * epsilon_x_data = data_ptr(epsilon, y, x);
        do_add(epsilon_x_data, CudaNdarray_DEV_DATA(Dy_p), CudaNdarray_SIZE(Dy_p));

        Py_XDECREF(Dy_p);
        Py_XDECREF(y_p);
      }

      do_lstm_bwd(delta, epsilon, %(Y)s, %(Dd)s, %(c)s, y, x, rightBorder, %(i)s);
    }

    %(DW_re)s = CudaNdarray_uninitialized_like(%(W_re)s);
    //DW_re = Y[0..end-1]^T * delta[1..end]
    affine_global(%(Y)s, delta, %(DW_re)s, true, false, 1, 0.0f);
    //DW_re += y0^T * delta[0]
    affine_y_x(0, 0, %(y0)s, 0, 0, delta, 0, 0, %(DW_re)s, true, false);

    %(DZ)s = delta;

    %(Dc)s = CudaNdarray_uninitialized_like(%(c)s);
    HANDLE_ERROR(cudaMemcpy(CudaNdarray_DEV_DATA(%(Dc)s), CudaNdarray_DEV_DATA(epsilon),
      Y_dim[1]*Y_dim[2]*sizeof(float), cudaMemcpyDeviceToDevice));

    %(Dy0)s = CudaNdarray_zeros_like(%(y0)s);
    //calculation like epsilon
    affine_y_x(0, 0, delta, 0, 0, %(W_re)s, 0, 0, %(Dy0)s, false, true);
    //add custom function
    //TODO: move to function
    PyObject * delta_x_obj = PyObject_CallMethod((PyObject*) delta, "__getitem__", "(i)", 0);
    assert(delta_x_obj);
    CudaNdarray * delta_x = (CudaNdarray*) delta_x_obj;

    //note: the missing comma is intentionally for the case where there are 0 custom inputs
    std::vector<PyObject*> res_vec = %(bwd_fun)s(%(y0)s, delta_x %(custom_inputs_str)s);
    assert(res_vec.size() == 1);
    Py_XDECREF(delta_x);
    CudaNdarray * Dy_p = (CudaNdarray*) res_vec[0];

    //copy to Dy0
    do_add(CudaNdarray_DEV_DATA(%(Dy0)s), CudaNdarray_DEV_DATA(Dy_p), CudaNdarray_SIZE(Dy_p));

    Py_XDECREF(Dy_p);


    if(!%(inplace)s)
    {
      Py_XDECREF(epsilon);
    }

    """ % locals()

#------------------------

class LSTMCustomOp(theano.sandbox.cuda.GpuOp):
  __props__ = ("inplace", "fun_name")

  def __init__(self, fun_name):
    super(LSTMCustomOp, self).__init__(self)
    self.inplace = False
    self.fun_name = fun_name

  #TODO: make recurrent weights customizable, atm fixed to single matrix (W_re)
  #B is base
  def make_node(self, Z, c, y0, i, W_re, *custom_inputs):
    from Device import have_gpu
    assert have_gpu()

    custom_inputs = [gpu_contiguous(as_cuda_ndarray_variable(x)) for x in custom_inputs]
    Z = gpu_contiguous(as_cuda_ndarray_variable(Z))
    c = gpu_contiguous(as_cuda_ndarray_variable(c))
    y0 = gpu_contiguous(as_cuda_ndarray_variable(y0))
    i = gpu_contiguous(as_cuda_ndarray_variable(T.cast(i,'float32')))
    W_re = gpu_contiguous(as_cuda_ndarray_variable(W_re))
    assert Z.dtype == "float32"
    assert c.dtype == "float32"
    assert y0.dtype == "float32"
    assert W_re.dtype == "float32"
    for x in custom_inputs:
      assert x.dtype == "float32"
    assert Z.ndim == 3
    assert c.ndim == 2
    assert y0.ndim == 2
    assert i.ndim == 2
    assert W_re.ndim == 2

    #results: output Y, (gates and cell state) H
    return theano.Apply(self, [Z, c, y0, i, W_re] + custom_inputs, [Z.type(), Z.type(), c.type()])

  def c_support_code(self):
    #do not remove this import as it is used in the c code
    import CustomLSTMFunctions
    funloader = make_funloader_code(self.fun_name + "_fwd", 1)
    crnn_path = os.path.dirname(__file__)
    with open(crnn_path + "/c_support_code_mdlstm.cpp") as f:
      return funloader + f.read()

  def c_code(self, node, name, input_names, output_names, sub):
    Z, c, y0, i, W_re = input_names[:5]
    custom_inputs = input_names[5:]
    custom_inputs_str = ("," if len(custom_inputs) > 0 else "") + ",".join(custom_inputs)
    Y, H, d = output_names
    fwd_fun = self.fun_name + "_fwd"
    fail = sub['fail']
    return """
    //std::cout << "LSTMCustomOp called" << std::endl;
    if(%(Y)s || %(H)s || %(d)s)
    {
      printf("Y or H or d already exist\\n");
      //TODO check if we can reuse it
      Py_XDECREF(%(Y)s);
      Py_XDECREF(%(H)s);
      Py_XDECREF(%(d)s);
    }

    const int * Z_dim = CudaNdarray_HOST_DIMS(%(Z)s);
    const int dims_Y[] = {Z_dim[0], Z_dim[1], Z_dim[2] / 4};
    const int dims_H[] = {Z_dim[0], Z_dim[1], Z_dim[2]};
    const int dims_d[] = {Z_dim[1], Z_dim[2] / 4};
    int size_d = Z_dim[1] * Z_dim[2] / 4;

    %(Y)s = (CudaNdarray*) CudaNdarray_NewDims(3,dims_Y);
    %(d)s = (CudaNdarray*) CudaNdarray_NewDims(2, dims_d);
    %(H)s = (CudaNdarray*) CudaNdarray_NewDims(3,dims_H); //CudaNdarray_uninitialized_like(%(Z)s);
    cudaMemcpy(CudaNdarray_DEV_DATA(%(H)s), CudaNdarray_DEV_DATA(%(Z)s),
      dims_H[0]*dims_H[1]*dims_H[2]*sizeof(float), cudaMemcpyDeviceToDevice);

    int y = 0;
    for(int x = 0; x < Z_dim[0]; ++x)
    {
      bool leftBorder = (x == 0);
      if(leftBorder)
      {
        affine_y_x(y, x-1, %(y0)s, y, x, %(W_re)s, y, x, %(H)s);
      }
      else
      {
        affine_y_x(y, x-1, %(Y)s, y, x, %(W_re)s, y, x, %(H)s);
      }

      //call custom function here
      CudaNdarray * y_p = 0;
      if(leftBorder)
      {
        y_p = %(y0)s;
      }
      else
      {
        PyObject * y_p_obj = PyObject_CallMethod((PyObject*) %(Y)s, "__getitem__", "(i)", x-1);
        assert(y_p_obj);
        y_p = (CudaNdarray*) y_p_obj;
      }

      std::vector<PyObject*> res_vec = %(fwd_fun)s(y_p %(custom_inputs_str)s);
      assert(res_vec.size() == 1);
      CudaNdarray * res = (CudaNdarray*) res_vec[0];

      //add to H
      float * H_y_x_data = data_ptr(%(H)s, y, x);
      do_add(H_y_x_data, CudaNdarray_DEV_DATA(res), CudaNdarray_SIZE(res));

      if(!leftBorder)
      {
        Py_XDECREF(y_p);
      }
      Py_XDECREF(res);


      float * d_ptr = (x == Z_dim[0] - 1) ? CudaNdarray_DEV_DATA(%(d)s) : 0;
      do_lstm(%(H)s, %(Y)s, %(c)s, d_ptr, y, x, %(i)s);
    }
    """ % locals()

  def grad(self, inputs, output_grads):
    Z, c, y0, i, W_re = inputs[:5]
    custom_inputs = inputs[5:]
    DY, DH, Dd = output_grads

    Z_raw = Z.owner.inputs[0].owner.inputs[0]
    c_raw = c.owner.inputs[0].owner.inputs[0]
    y0_raw = y0.owner.inputs[0].owner.inputs[0]
    i_raw = i.owner.inputs[0].owner.inputs[0]
    W_re_raw = W_re.owner.inputs[0]
    custom_inputs_raw = [x.owner.inputs[0] for x in custom_inputs]
    #we have to make sure that this in only computed once!
    #for this we have to extract the raw variables before conversion to continuous gpu array
    #so that theano can merge the nodes
    Y, H, d = self(*([Z_raw, c_raw, y0_raw, i_raw, W_re_raw] + custom_inputs))
    if isinstance(DY.type, theano.gradient.DisconnectedType):
      DY = T.zeros_like(Z)
    if isinstance(Dd.type, theano.gradient.DisconnectedType):
      Dd = T.zeros_like(c)
    #TODO: later also pass B
    DZ, Dc, Dy0, DW_re = LSTMCustomOpGrad(fun_name=self.fun_name)(*([Y, H, c, y0, i, Dd, DY, W_re] + custom_inputs))
    Di = theano.gradient.grad_undefined(self, 3, inputs[3], 'cannot diff w.r.t. index')
    #TODO
    Dcustom = [theano.gradient.grad_undefined(self, 5 + idx, inputs[5+idx], 'cannot diff w.r.t. custom yet')
               for idx in xrange(len(custom_inputs))]

    return [DZ, Dc, Dy0, Di, DW_re] + Dcustom

LSTMCustomTestOpInstance = LSTMCustomOp(fun_name="test_fun")
LSTMCustomDotAttentionOpInstance = LSTMCustomOp(fun_name="attention_dot_fun")