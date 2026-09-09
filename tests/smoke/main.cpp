#include <MNN/expr/ExprCreator.hpp>
#include <MNN/Interpreter.hpp>
#include <MNN/Tensor.hpp>
#include <cmath>
#include <iostream>
#include <map>
#include <memory>
#include <string>

int main(int argc, char** argv) {
    using namespace MNN::Express;
    const std::map<std::string, MNNForwardType> backends = {
        {"cpu", MNN_FORWARD_CPU}, {"vulkan", MNN_FORWARD_VULKAN},
        {"opencl", MNN_FORWARD_OPENCL}, {"cuda", MNN_FORWARD_CUDA},
        {"metal", MNN_FORWARD_METAL}, {"coreml", MNN_FORWARD_NN},
        {"opengl", MNN_FORWARD_OPENGL}};
    const std::string name = argc > 1 ? argv[1] : "cpu";
    const auto requested = backends.find(name);
    if (requested == backends.end()) return 4;
    // Serialize one complete graph on CPU before selecting the execution device.
    // CoreML consumes a whole model, not independently evaluated constant expressions.
    auto input = _Input({1, 1, 2, 2}, NCHW, halide_type_of<float>());
    input->setName("input");
    auto result = _Add(_Multiply(input, _Scalar<float>(2.0f)), _Scalar<float>(1.0f));
    result->setName("output");
    const auto model = Variable::save({result});
    std::unique_ptr<MNN::Interpreter> interpreter(MNN::Interpreter::createFromBuffer(model.data(), model.size()));
    if (!interpreter) return 1;
    MNN::BackendConfig backendConfig;
    backendConfig.precision = MNN::BackendConfig::Precision_High;
    MNN::ScheduleConfig config;
    config.type = requested->second;
    config.numThread = 1;
    config.backendConfig = &backendConfig;
    const auto runtime = MNN::Interpreter::createRuntime({config});
    if (runtime.first.find(requested->second) == runtime.first.end()) {
        std::cerr << "Requested backend is unavailable: " << name << "\n";
        return 5;
    }
    auto session = interpreter->createSession(config, runtime);
    if (!session) return 6;
    int activeBackends[2] = {-1, -1};
    if (!interpreter->getSessionInfo(session, MNN::Interpreter::BACKENDS, activeBackends)
        || activeBackends[0] != requested->second) return 7;
    auto sessionInput = interpreter->getSessionInput(session, "input");
    auto sessionOutput = interpreter->getSessionOutput(session, "output");
    if (!sessionInput || !sessionOutput) return 8;
    MNN::Tensor hostInput(sessionInput, MNN::Tensor::CAFFE);
    for (int i = 0; i < 4; ++i) hostInput.host<float>()[i] = float(i + 1);
    if (!sessionInput->copyFromHostTensor(&hostInput)) return 9;
    if (interpreter->runSession(session) != MNN::NO_ERROR) return 10;
    MNN::Tensor hostOutput(sessionOutput, MNN::Tensor::CAFFE);
    if (!sessionOutput->copyToHostTensor(&hostOutput)) return 11;
    auto output = hostOutput.host<float>();
    if (!output || hostOutput.elementSize() != 4) return 2;
    for (int i = 0; i < 4; ++i) {
        if (!std::isfinite(output[i]) || std::abs(output[i] - float(2 * (i + 1) + 1)) > 1e-5f) return 3;
    }
    std::cout << "MNN " << name << " session inference passed: 3, 5, 7, 9\n";
    return 0;
}
