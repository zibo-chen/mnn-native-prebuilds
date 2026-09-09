#include <MNN/expr/ExprCreator.hpp>
#include <cmath>
#include <iostream>

int main() {
    using namespace MNN::Express;
    auto input = _Input({1, 4}, NCHW, halide_type_of<float>());
    auto values = input->writeMap<float>();
    if (!values) return 1;
    for (int i = 0; i < 4; ++i) values[i] = float(i + 1);
    auto result = _Add(_Multiply(input, _Scalar<float>(2.0f)), _Scalar<float>(1.0f));
    auto output = result->readMap<float>();
    if (!output) return 2;
    for (int i = 0; i < 4; ++i) {
        if (std::abs(output[i] - float(2 * (i + 1) + 1)) > 1e-5f) return 3;
    }
    std::cout << "MNN CPU expression inference passed: 3, 5, 7, 9\n";
    return 0;
}
