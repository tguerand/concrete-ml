"""Tests for the torch to numpy module."""
import io
import tempfile
import zipfile
from functools import partial
from inspect import signature
from pathlib import Path

import numpy
import onnx
import onnxruntime as ort
import pytest
import torch
import torch.quantization
from torch import nn

from concrete.ml.onnx.convert import OPSET_VERSION_FOR_ONNX_EXPORT
from concrete.ml.pytest.torch_models import (
    FC,
    BranchingGemmModule,
    BranchingModule,
    CNNGrouped,
    CNNOther,
    FCSmall,
    MultiInputNN,
    NetWithLoops,
    SimpleQAT,
    SingleMixNet,
    StepActivationModule,
    UnivariateModule,
)
from concrete.ml.quantization import QuantizedModule

# pylint sees separated imports from concrete but does not understand they come from two different
# packages/projects, disable the warning
# pylint: disable=ungrouped-imports
from concrete.ml.torch.compile import (
    compile_brevitas_qat_model,
    compile_onnx_model,
    compile_torch_model,
)

# pylint: enable=ungrouped-imports

# INPUT_OUTPUT_FEATURE is the number of input and output of each of the network layers.
# (as well as the input of the network itself)
# Note that when comparing two predictions with few features the r2 score is brittle
# thus we prefer to avoid values that are too low (e.g. 1, 2)
INPUT_OUTPUT_FEATURE = [5]


# pylint: disable-next=too-many-arguments
def compile_and_test_torch_or_onnx(  # pylint: disable=too-many-locals, too-many-statements
    input_output_feature,
    model_class,
    activation_function,
    qat_bits,
    default_configuration,
    use_virtual_lib,
    is_onnx,
    check_is_good_execution_for_cml_vs_circuit,
    dump_onnx=False,
    expected_onnx_str=None,
    verbose_compilation=False,
) -> QuantizedModule:
    """Test the different model architecture from torch numpy."""

    # Define an input shape (n_examples, n_features)
    n_examples = 500

    # Define the torch model
    if not isinstance(input_output_feature, tuple):
        input_output_feature = (input_output_feature,)

    torch_model = model_class(
        input_output=input_output_feature[0], activation_function=activation_function
    )

    num_inputs = len(signature(torch_model.forward).parameters)

    # Create random input
    inputset = (
        tuple(
            numpy.random.uniform(-100, 100, size=(n_examples, *input_output_feature))
            for _ in range(num_inputs)
        )
        if num_inputs > 1
        else numpy.random.uniform(-100, 100, size=(n_examples, *input_output_feature))
    )

    # FHE vs Quantized are not done in the test anymore (see issue #177)
    if not use_virtual_lib:

        n_bits = (
            {"model_inputs": 2, "model_outputs": 2, "op_inputs": 2, "op_weights": 2}
            if qat_bits == 0
            else qat_bits
        )

        if is_onnx:

            output_onnx_file_path = Path(tempfile.mkstemp(suffix=".onnx")[1])
            inputset_as_numpy_tuple = (
                (val for val in inputset) if isinstance(inputset, tuple) else (inputset,)
            )
            dummy_input = tuple(
                torch.from_numpy(val[[0], ::]).float() for val in inputset_as_numpy_tuple
            )
            torch.onnx.export(
                torch_model,
                dummy_input,
                str(output_onnx_file_path),
                opset_version=OPSET_VERSION_FOR_ONNX_EXPORT,
            )
            onnx_model = onnx.load_model(str(output_onnx_file_path))
            onnx.checker.check_model(onnx_model)

            quantized_numpy_module = compile_onnx_model(
                onnx_model,
                inputset,
                import_qat=qat_bits != 0,
                configuration=default_configuration,
                n_bits=n_bits,
                use_virtual_lib=use_virtual_lib,
                verbose_compilation=verbose_compilation,
            )
        else:
            quantized_numpy_module = compile_torch_model(
                torch_model,
                inputset,
                import_qat=qat_bits != 0,
                configuration=default_configuration,
                n_bits=n_bits,
                use_virtual_lib=use_virtual_lib,
                verbose_compilation=verbose_compilation,
            )

        # Create test data from the same distribution and quantize using
        # learned quantization parameters during compilation
        x_test = tuple(
            numpy.random.uniform(-100, 100, size=(1, *input_output_feature))
            for _ in range(num_inputs)
        )

        qtest = quantized_numpy_module.quantize_input(*x_test)

        if not isinstance(qtest, tuple):
            qtest = (qtest,)

        quantized_numpy_module.check_model_is_compiled()

        # Make sure VL and quantized module forward give the same output.
        check_is_good_execution_for_cml_vs_circuit(
            qtest, model_function=quantized_numpy_module, simulate=use_virtual_lib
        )
    else:
        # Compile our network with 16-bits
        # to compare to torch (8b weights + float 32 activations)
        if qat_bits == 0:
            n_bits = 16
        else:
            n_bits = {
                "model_inputs": 16,
                "op_weights": qat_bits,
                "op_inputs": qat_bits,
                "model_outputs": 16,
            }

        quantized_numpy_module = compile_torch_model(
            torch_model,
            inputset,
            import_qat=qat_bits != 0,
            configuration=default_configuration,
            n_bits=n_bits,
            use_virtual_lib=use_virtual_lib,
            verbose_compilation=verbose_compilation,
        )

        accuracy_test_rounding(
            torch_model,
            quantized_numpy_module,
            inputset,
            import_qat=qat_bits != 0,
            configuration=default_configuration,
            n_bits=n_bits,
            use_virtual_lib=use_virtual_lib,
            verbose_compilation=verbose_compilation,
            input_output_feature=input_output_feature,
            num_inputs=num_inputs,
            check_is_good_execution_for_cml_vs_circuit=check_is_good_execution_for_cml_vs_circuit,
        )

        onnx_model = quantized_numpy_module.onnx_model

        if dump_onnx:
            str_model = onnx.helper.printable_graph(onnx_model.graph)
            print("ONNX model:")
            print(str_model)
            assert str_model == expected_onnx_str

    return quantized_numpy_module


# pylint: disable-next=too-many-arguments,too-many-locals
def accuracy_test_rounding(
    torch_model,
    quantized_numpy_module,
    inputset,
    import_qat,
    configuration,
    n_bits,
    use_virtual_lib,
    verbose_compilation,
    input_output_feature,
    num_inputs,
    check_is_good_execution_for_cml_vs_circuit,
):
    """Check rounding behavior.

    The original quantized_numpy_module, compiled over the torch_model without rounding is
    compared against quantized_numpy_module_round_low_precision, the torch_model compiled with
    a rounding threshold of 2 bits, and quantized_numpy_module_round_high_precision,
    the torch_model compiled with maximum bit-width computed in the quantized_numpy_module - 1.

    The final assertion tests whether the mean absolute error between
    quantized_numpy_module_round_high_precision and quantized_numpy_module is lower than
    quantized_numpy_module_round_low_precision and quantized_numpy_module making sure that the
    rounding feature has the expected behavior on the model accuracy.
    """
    # FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/2888
    assert use_virtual_lib, "Rounding is not available in FHE yet."

    # Check that the maximum_integer_bit_width is at least 4 bits to compare the rounding
    # feature with enough precision.
    assert quantized_numpy_module.fhe_circuit.graph.maximum_integer_bit_width() >= 4

    # Compile with a rounding threshold equal to the maximum bit-width
    # computed in the original quantized_numpy_module
    quantized_numpy_module_round_high_precision = compile_torch_model(
        torch_model,
        inputset,
        import_qat=import_qat,
        configuration=configuration,
        n_bits=n_bits,
        use_virtual_lib=use_virtual_lib,
        verbose_compilation=verbose_compilation,
        rounding_threshold_bits=quantized_numpy_module.fhe_circuit.graph.maximum_integer_bit_width()
        - 1,
    )

    # and another quantized module with a rounding threshold equal to 2 bits
    quantized_numpy_module_round_low_precision = compile_torch_model(
        torch_model,
        inputset,
        import_qat=import_qat,
        configuration=configuration,
        n_bits=n_bits,
        use_virtual_lib=use_virtual_lib,
        verbose_compilation=verbose_compilation,
        rounding_threshold_bits=2,
    )

    n_examples_test = 100
    # Create some fake data
    x_test = tuple(
        numpy.random.uniform(-100, 100, size=(n_examples_test, *input_output_feature))
        for _ in range(num_inputs)
    )

    # Make sure the two modules have the same quantization result

    qtest = quantized_numpy_module.quantize_input(*x_test)
    qtest_high = quantized_numpy_module_round_high_precision.quantize_input(*x_test)
    qtest_low = quantized_numpy_module_round_low_precision.quantize_input(*x_test)
    numpy.testing.assert_array_equal(qtest, qtest_high)
    numpy.testing.assert_array_equal(qtest, qtest_low)

    if not isinstance(qtest, tuple):
        qtest = (qtest,)

    results = []
    results_high_precision = []
    results_low_precision = []
    for i in range(n_examples_test):

        # Extract the i th example for each tensor in the tuple qtest
        # while keeping the dimension of the original tensors.
        # e.g. if qtest is a tuple of two (100, 10) tensors
        # then q_x becomes a tuple of two tensors of shape (1, 10).
        q_x = tuple(q[[i]] for q in qtest)

        # encrypt, run, and decrypt with different precision modes
        # FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/2888
        q_result = quantized_numpy_module.fhe_circuit.simulate(*q_x)
        q_result_high_precision = quantized_numpy_module_round_high_precision.fhe_circuit.simulate(
            *q_x
        )
        q_result_low_precision = quantized_numpy_module_round_low_precision.fhe_circuit.simulate(
            *q_x
        )

        # dequantize the results to obtain the actual output values
        result = quantized_numpy_module.dequantize_output(q_result)
        result_high_precision = quantized_numpy_module_round_high_precision.dequantize_output(
            q_result_high_precision
        )
        result_low_precision = quantized_numpy_module_round_low_precision.dequantize_output(
            q_result_low_precision
        )

        # append the results to respective lists
        results.append(result)
        results_high_precision.append(result_high_precision)
        results_low_precision.append(result_low_precision)

    # Check modules predictions VL vs CML.
    check_is_good_execution_for_cml_vs_circuit(
        qtest, quantized_numpy_module, simulate=use_virtual_lib
    )
    check_is_good_execution_for_cml_vs_circuit(
        qtest, quantized_numpy_module_round_high_precision, simulate=use_virtual_lib
    )
    check_is_good_execution_for_cml_vs_circuit(
        qtest, quantized_numpy_module_round_low_precision, simulate=use_virtual_lib
    )

    # Check that high precision gives a better match than low precision
    mae_high_precision = numpy.abs(
        [results[i] - results_high_precision[i] for i in range(len(results))]
    ).mean()
    mae_low_precision = numpy.abs(
        [results[i] - results_low_precision[i] for i in range(len(results))]
    ).mean()
    assert mae_high_precision <= mae_low_precision, "Rounding is not working as expected."


@pytest.mark.parametrize(
    "activation_function",
    [
        pytest.param(nn.ReLU, id="relu"),
    ],
)
@pytest.mark.parametrize(
    "model",
    [
        pytest.param(FCSmall),
        pytest.param(partial(NetWithLoops, n_fc_layers=2)),
        pytest.param(BranchingModule),
        pytest.param(BranchingGemmModule),
        pytest.param(MultiInputNN),
        pytest.param(UnivariateModule),
        pytest.param(StepActivationModule),
    ],
)
@pytest.mark.parametrize(
    "input_output_feature",
    [pytest.param(input_output_feature) for input_output_feature in INPUT_OUTPUT_FEATURE],
)
@pytest.mark.parametrize("use_virtual_lib", [True, False], ids=["virtual", "FHE"])
@pytest.mark.parametrize("is_onnx", [True, False], ids=["is_onnx", ""])
def test_compile_torch_or_onnx_networks(
    input_output_feature,
    model,
    activation_function,
    default_configuration,
    use_virtual_lib,
    is_onnx,
    check_is_good_execution_for_cml_vs_circuit,
    is_weekly_option,
):
    """Test the different model architecture from torch numpy."""

    # Avoid too many tests
    if use_virtual_lib and not is_weekly_option:
        if model not in [FCSmall, BranchingModule]:
            pytest.skip("Avoid too many tests")

    # To signal that this network is not using QAT set the QAT bits to 0
    qat_bits = 0

    compile_and_test_torch_or_onnx(
        input_output_feature,
        model,
        activation_function,
        qat_bits,
        default_configuration,
        use_virtual_lib,
        is_onnx,
        check_is_good_execution_for_cml_vs_circuit,
        verbose_compilation=False,
    )


@pytest.mark.parametrize(
    "activation_function",
    [
        pytest.param(nn.ReLU, id="relu"),
    ],
)
@pytest.mark.parametrize(
    "model",
    [
        pytest.param(CNNOther),
        pytest.param(partial(CNNGrouped, groups=3)),
    ],
)
@pytest.mark.parametrize("use_virtual_lib", [True, False])
@pytest.mark.parametrize("is_onnx", [True, False])
def test_compile_torch_or_onnx_conv_networks(  # pylint: disable=unused-argument
    model,
    activation_function,
    default_configuration,
    use_virtual_lib,
    is_onnx,
    check_graph_input_has_no_tlu,
    check_graph_output_has_no_tlu,
    check_is_good_execution_for_cml_vs_circuit,
):
    """Test the different model architecture from torch numpy."""

    # To signal that this network is not using QAT set the QAT bits to 0
    qat_bits = 0

    q_module = compile_and_test_torch_or_onnx(
        (6, 7, 7),
        model,
        activation_function,
        qat_bits,
        default_configuration,
        use_virtual_lib,
        is_onnx,
        check_is_good_execution_for_cml_vs_circuit,
        verbose_compilation=False,
    )

    check_graph_input_has_no_tlu(q_module.fhe_circuit.graph)
    check_graph_output_has_no_tlu(q_module.fhe_circuit.graph)


@pytest.mark.parametrize(
    "activation_function",
    [
        pytest.param(nn.Sigmoid, id="sigmoid"),
        pytest.param(nn.ReLU, id="relu"),
        pytest.param(nn.ReLU6, id="relu6"),
        pytest.param(nn.Tanh, id="tanh"),
        pytest.param(nn.ELU, id="ELU"),
        pytest.param(nn.Hardsigmoid, id="Hardsigmoid"),
        pytest.param(nn.Hardtanh, id="Hardtanh"),
        pytest.param(nn.LeakyReLU, id="LeakyReLU"),
        pytest.param(nn.SELU, id="SELU"),
        pytest.param(nn.CELU, id="CELU"),
        pytest.param(nn.Softplus, id="Softplus"),
        pytest.param(nn.PReLU, id="PReLU"),
        pytest.param(nn.Hardswish, id="Hardswish"),
        pytest.param(nn.SiLU, id="SiLU"),
        pytest.param(nn.Mish, id="Mish"),
        pytest.param(nn.Tanhshrink, id="Tanhshrink"),
        pytest.param(partial(nn.Threshold, threshold=0, value=0), id="Threshold"),
        pytest.param(nn.Softshrink, id="Softshrink"),
        pytest.param(nn.Hardshrink, id="Hardshrink"),
        pytest.param(nn.Softsign, id="Softsign"),
        pytest.param(nn.GELU, id="GELU"),
        pytest.param(nn.LogSigmoid, id="LogSigmoid"),
        # Some issues are still encountered with some activations
        # FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/335
        #
        # Other problems, certainly related to tests:
        # Required positional arguments: 'embed_dim' and 'num_heads' and fails with a partial
        # pytest.param(nn.MultiheadAttention, id="MultiheadAttention"),
        # Activation with a RandomUniformLike
        # pytest.param(nn.RReLU, id="RReLU"),
        # Halving dimension must be even, but dimension 3 is size 3
        # pytest.param(nn.GLU, id="GLU"),
    ],
)
@pytest.mark.parametrize(
    "model",
    [
        pytest.param(FCSmall),
    ],
)
@pytest.mark.parametrize(
    "input_output_feature",
    [pytest.param(input_output_feature) for input_output_feature in INPUT_OUTPUT_FEATURE],
)
@pytest.mark.parametrize("use_virtual_lib", [True, False])
@pytest.mark.parametrize("is_onnx", [True, False])
def test_compile_torch_or_onnx_activations(
    input_output_feature,
    model,
    activation_function,
    default_configuration,
    use_virtual_lib,
    is_onnx,
    check_is_good_execution_for_cml_vs_circuit,
):
    """Test the different model architecture from torch numpy."""

    # To signal that this network is not using QAT set the QAT bits to 0
    qat_bits = 0

    compile_and_test_torch_or_onnx(
        input_output_feature,
        model,
        activation_function,
        qat_bits,
        default_configuration,
        use_virtual_lib,
        is_onnx,
        check_is_good_execution_for_cml_vs_circuit,
        verbose_compilation=False,
    )


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(SimpleQAT),
    ],
)
@pytest.mark.parametrize(
    "input_output_feature",
    [pytest.param(input_output_feature) for input_output_feature in [2, 4]],
)
@pytest.mark.parametrize(
    "n_bits",
    [pytest.param(n_bits) for n_bits in [1, 2]],
)
@pytest.mark.parametrize("use_virtual_lib", [True, False])
def test_compile_torch_qat(
    input_output_feature,
    model,
    n_bits,
    default_configuration,
    use_virtual_lib,
    check_is_good_execution_for_cml_vs_circuit,
):
    """Test the different model architecture from torch numpy."""

    model = partial(model, n_bits=n_bits)

    # Import these networks from torch directly
    is_onnx = False
    qat_bits = n_bits

    compile_and_test_torch_or_onnx(
        input_output_feature,
        model,
        nn.Sigmoid,
        qat_bits,
        default_configuration,
        use_virtual_lib,
        is_onnx,
        check_is_good_execution_for_cml_vs_circuit,
        verbose_compilation=False,
    )


@pytest.mark.parametrize(
    "model_class, expected_onnx_str",
    [
        pytest.param(
            FC,
            (
                """graph torch_jit (
  %onnx::Gemm_0[FLOAT, 1x7]
) initializers (
  %fc1.weight[FLOAT, 128x7]
  %fc1.bias[FLOAT, 128]
  %fc2.weight[FLOAT, 64x128]
  %fc2.bias[FLOAT, 64]
  %fc3.weight[FLOAT, 64x64]
  %fc3.bias[FLOAT, 64]
  %fc4.weight[FLOAT, 64x64]
  %fc4.bias[FLOAT, 64]
  %fc5.weight[FLOAT, 10x64]
  %fc5.bias[FLOAT, 10]
) {
  %/fc1/Gemm_output_0 = Gemm[alpha = 1, beta = 1, transB = 1]"""
                """(%onnx::Gemm_0, %fc1.weight, %fc1.bias)
  %/act_1/Relu_output_0 = Relu(%/fc1/Gemm_output_0)
  %/fc2/Gemm_output_0 = Gemm[alpha = 1, beta = 1, transB = 1]"""
                """(%/act_1/Relu_output_0, %fc2.weight, %fc2.bias)
  %/act_2/Relu_output_0 = Relu(%/fc2/Gemm_output_0)
  %/fc3/Gemm_output_0 = Gemm[alpha = 1, beta = 1, transB = 1]"""
                """(%/act_2/Relu_output_0, %fc3.weight, %fc3.bias)
  %/act_3/Relu_output_0 = Relu(%/fc3/Gemm_output_0)
  %/fc4/Gemm_output_0 = Gemm[alpha = 1, beta = 1, transB = 1]"""
                """(%/act_3/Relu_output_0, %fc4.weight, %fc4.bias)
  %/act_4/Relu_output_0 = Relu(%/fc4/Gemm_output_0)
  %19 = Gemm[alpha = 1, beta = 1, transB = 1](%/act_4/Relu_output_0, %fc5.weight, %fc5.bias)
  return %19
}"""
            ),
        ),
    ],
)
@pytest.mark.parametrize(
    "activation_function",
    [
        pytest.param(nn.ReLU, id="relu"),
    ],
)
def test_dump_torch_network(
    model_class,
    expected_onnx_str,
    activation_function,
    default_configuration,
    check_is_good_execution_for_cml_vs_circuit,
):
    """This is a test which is equivalent to tests in test_dump_onnx.py, but for torch modules."""
    input_output_feature = 7
    use_virtual_lib = True
    is_onnx = False
    qat_bits = 0

    compile_and_test_torch_or_onnx(
        input_output_feature,
        model_class,
        activation_function,
        qat_bits,
        default_configuration,
        use_virtual_lib,
        is_onnx,
        check_is_good_execution_for_cml_vs_circuit,
        dump_onnx=True,
        expected_onnx_str=expected_onnx_str,
        verbose_compilation=False,
    )


@pytest.mark.parametrize("verbose_compilation", [True, False])
# pylint: disable-next=too-many-locals
def test_pretrained_mnist_qat(
    default_configuration,
    check_accuracy,
    verbose_compilation,
    check_graph_output_has_no_tlu,
    check_is_good_execution_for_cml_vs_circuit,
):
    """Load a QAT MNIST model and make sure we get the same results in VL as with ONNX."""

    onnx_file_path = "tests/data/mnist_2b_s1_1.zip"
    mnist_test_path = "tests/data/mnist_test_batch.zip"

    # Load ONNX model from zip file
    with zipfile.ZipFile(onnx_file_path, "r") as archive_model:
        onnx_model_serialized = io.BytesIO(archive_model.read("mnist_2b_s1_1.onnx")).read()
        onnx_model = onnx.load_model_from_string(onnx_model_serialized)

    onnx.checker.check_model(onnx_model)

    # Load test data and ground truth from zip file
    with zipfile.ZipFile(mnist_test_path, "r") as archive_data:
        mnist_data = numpy.load(
            io.BytesIO(archive_data.read("mnist_test_batch.npy")), allow_pickle=True
        ).item()

    # Get the test data
    inputset = mnist_data["test_data"]

    # Run through ONNX runtime and collect results
    ort_session = ort.InferenceSession(onnx_model_serialized)

    onnx_results = numpy.zeros((inputset.shape[0],), dtype=numpy.int64)
    for i, x_test in enumerate(inputset):
        onnx_outputs = ort_session.run(
            None,
            {onnx_model.graph.input[0].name: x_test.reshape(1, -1)},
        )
        onnx_results[i] = numpy.argmax(onnx_outputs[0])

    # Compile to Concrete ML with the Virtual Library, with a high bitwidth
    n_bits = {
        "model_inputs": 16,
        "op_weights": 2,
        "op_inputs": 2,
        "model_outputs": 16,
    }

    quantized_numpy_module = compile_onnx_model(
        onnx_model,
        inputset,
        import_qat=True,
        configuration=default_configuration,
        n_bits=n_bits,
        use_virtual_lib=True,
        verbose_compilation=verbose_compilation,
    )

    num_inputs = 1

    # Create test data tuple
    x_test = tuple(inputset for _ in range(num_inputs))

    # Check the forward works with the high bitwidth
    qtest = quantized_numpy_module.quantize_input(*x_test)

    if not isinstance(qtest, tuple):
        qtest = (qtest,)

    quantized_numpy_module.check_model_is_compiled()

    check_is_good_execution_for_cml_vs_circuit(qtest, quantized_numpy_module, simulate=True)

    # Collect VL results
    results = []
    for i in range(inputset.shape[0]):

        # Extract the i th example for each tensor in the tuple qtest
        # while keeping the dimension of the original tensors.
        # e.g. if qtest is a tuple of two (100, 10) tensors
        # then q_x becomes a tuple of two tensors of shape (1, 10).
        q_x = tuple(q[[i]] for q in qtest)
        q_result = quantized_numpy_module.fhe_circuit.simulate(*q_x)
        result = quantized_numpy_module.dequantize_output(q_result)
        result = numpy.argmax(result)
        results.append(result)

    # Compare ONNX runtime vs Virtual Lib
    check_accuracy(onnx_results, results, threshold=0.999)

    # Make sure absolute accuracy is good, this model should have at least 90% accuracy
    check_accuracy(mnist_data["gt"], results, threshold=0.9)

    # Compile to Concrete ML with the Virtual Library with a FHE compatible bitwidth
    n_bits = {
        "model_inputs": 7,
        "op_weights": 2,
        "op_inputs": 2,
        "model_outputs": 7,
    }

    quantized_numpy_module = compile_onnx_model(
        onnx_model,
        inputset,
        import_qat=True,
        configuration=default_configuration,
        n_bits=n_bits,
        use_virtual_lib=False,
        verbose_compilation=verbose_compilation,
    )

    # As this is a custom QAT network, the input goes through multiple univariate
    # ops that form a quantizer. Thus it has input TLUs. But it should not have output TLUs
    check_graph_output_has_no_tlu(quantized_numpy_module.fhe_circuit.graph)

    assert quantized_numpy_module.fhe_circuit.graph.maximum_integer_bit_width() <= 8


def test_qat_import_bits_check(default_configuration):
    """Test that compile_brevitas_qat_model does not need an n_bits config."""

    input_features = 10

    model = SingleMixNet(False, True, 10, 2)

    n_examples = 50

    # All these n_bits configurations should be valid
    # and produce the same result, as the input/output bit-widths for this network
    # are ignored due to the input/output TLU elimination
    n_bits_valid = [
        8,
        2,
        {"model_inputs": 8, "model_outputs": 8},
        {"model_inputs": 2, "model_outputs": 2},
    ]

    # Create random input
    inputset = numpy.random.uniform(-100, 100, size=(n_examples, input_features))

    # Compile with no quantization bitwidth, defaults are used
    quantized_numpy_module = compile_brevitas_qat_model(
        model,
        inputset,
        configuration=default_configuration,
        use_virtual_lib=True,
    )

    # Create test data from the same distribution and quantize using.
    n_examples_test = 100
    x_test = numpy.random.uniform(-100, 100, size=(n_examples_test, input_features))

    # The result of compiling without any n_bits (default)
    q_out = quantized_numpy_module.forward(quantized_numpy_module.quantize_input(x_test))

    # Compare the results of running with n_bits=None to the results running with
    # all the other n_bits configs. The results should be the same as bit-widths
    # are ignored for this network (they are overridden with Brevitas values stored in ONNX).
    for n_bits in n_bits_valid:
        quantized_numpy_module = compile_brevitas_qat_model(
            model,
            inputset,
            n_bits=n_bits,
            configuration=default_configuration,
            use_virtual_lib=True,
        )

        q_out_2 = quantized_numpy_module.forward(quantized_numpy_module.quantize_input(x_test))

        assert numpy.all(q_out == q_out_2)

    n_bits_invalid = [
        {"XYZ": 8, "model_inputs": 8},
        {"XYZ": 8},
    ]

    # Test that giving a dictionary with invalid keys does not work
    for n_bits in n_bits_invalid:
        with pytest.raises(AssertionError, match=".*n_bits can only contain the following keys.*"):
            quantized_numpy_module = compile_brevitas_qat_model(
                model,
                inputset,
                n_bits=n_bits,
                configuration=default_configuration,
                use_virtual_lib=True,
            )


def test_qat_import_check(default_configuration, check_is_good_execution_for_cml_vs_circuit):
    """Test two cases of custom (non brevitas) NNs where importing as QAT networks should fail."""
    qat_bits = 4

    use_virtual_lib = True

    error_message_pattern = "Error occurred during quantization aware training.*"

    # This first test is trying to import a network that is QAT (has a quantizer in the graph)
    # but the import bitwidth is wrong (mismatch between bitwidth specified in training
    # and the bitwidth specified during import). For NNs that are not built with Brevitas
    # the bitwidth must be manually specified and is used to infer quantization parameters.
    with pytest.raises(ValueError, match=error_message_pattern):
        compile_and_test_torch_or_onnx(
            10,
            partial(SimpleQAT, n_bits=6, disable_bit_check=True),
            nn.ReLU,
            qat_bits,
            default_configuration,
            use_virtual_lib,
            False,
            check_is_good_execution_for_cml_vs_circuit,
        )

    # The second case is a network that is not QAT but is being imported as a QAT network
    with pytest.raises(ValueError, match=error_message_pattern):
        compile_and_test_torch_or_onnx(
            (1, 7, 7),
            CNNOther,
            nn.ReLU,
            qat_bits,
            default_configuration,
            use_virtual_lib,
            False,
            check_is_good_execution_for_cml_vs_circuit,
        )

    class AllZeroCNN(CNNOther):
        """A CNN class that has all zero weights and biases."""

        def __init__(self, input_output, activation_function):
            super().__init__(input_output, activation_function)

            for m in self.modules():
                if isinstance(m, (nn.Conv2d, nn.Linear)):
                    torch.nn.init.constant_(m.weight.data, 0)
                    torch.nn.init.constant_(m.bias.data, 0)

    # A network that may look like QAT but it just zeros all inputs
    with pytest.raises(ValueError, match=error_message_pattern):
        compile_and_test_torch_or_onnx(
            (1, 7, 7),
            AllZeroCNN,
            nn.ReLU,
            qat_bits,
            default_configuration,
            use_virtual_lib,
            False,
            check_is_good_execution_for_cml_vs_circuit,
        )


@pytest.mark.parametrize("n_bits, use_virtual_lib", [(2, False)])
@pytest.mark.parametrize("use_qat", [True, False])
@pytest.mark.parametrize("force_tlu", [True, False])
@pytest.mark.parametrize("module, input_shape", [(SingleMixNet, (1, 8, 8)), (SingleMixNet, 10)])
def test_net_has_no_tlu(
    module,
    input_shape,
    use_qat,
    force_tlu,
    n_bits,
    use_virtual_lib,
    default_configuration,
    check_graph_output_has_no_tlu,
):
    """Tests that there is no TLU in nets with a single conv/linear."""
    use_conv = isinstance(input_shape, tuple) and len(input_shape) > 1

    net = module(use_conv, use_qat, input_shape, n_bits)

    # We have the option to force having a TLU in the net by
    # applying a nonlinear function on the original network's output. Thus
    # we can check that a tlu is indeed present and was not removed by accident in this case
    if force_tlu:

        def relu_adder_decorator(method):
            def decorate_name(self):
                return torch.relu(method(self))

            return decorate_name

        net.forward = relu_adder_decorator(net.forward)

    # Generate the input in both the 2d and 1d cases
    if not isinstance(input_shape, tuple):
        input_shape = (input_shape,)
    inputset = numpy.random.uniform(size=(100, *input_shape))

    if use_qat:
        # Compile with appropriate QAT compilation function, here the zero-points will all be 0
        quantized_numpy_module = compile_brevitas_qat_model(
            net,
            inputset,
            configuration=default_configuration,
            use_virtual_lib=use_virtual_lib,
        )
    else:
        # Compile with PTQ. Note that this will have zero-point>0
        quantized_numpy_module = compile_torch_model(
            net,
            inputset,
            import_qat=False,
            configuration=default_configuration,
            n_bits=n_bits,
            use_virtual_lib=use_virtual_lib,
        )

    mlir = quantized_numpy_module.fhe_circuit.mlir

    # Check if a TLU is present or not, depending on whether we force a TLU to be present
    if force_tlu:
        with pytest.raises(AssertionError):
            check_graph_output_has_no_tlu(quantized_numpy_module.fhe_circuit.graph)
        with pytest.raises(AssertionError):
            assert "lookup_table" not in mlir
    else:
        check_graph_output_has_no_tlu(quantized_numpy_module.fhe_circuit.graph)
        assert "lookup_table" not in mlir
