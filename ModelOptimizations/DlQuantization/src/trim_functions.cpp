//==============================================================================
//
//  @@-COPYRIGHT-START-@@
//
//  Copyright (c) 2016-2022, Qualcomm Innovation Center, Inc. All rights reserved.
//
//  Redistribution and use in source and binary forms, with or without
//  modification, are permitted provided that the following conditions are met:
//
//  1. Redistributions of source code must retain the above copyright notice,
//     this list of conditions and the following disclaimer.
//
//  2. Redistributions in binary form must reproduce the above copyright notice,
//     this list of conditions and the following disclaimer in the documentation
//     and/or other materials provided with the distribution.
//
//  3. Neither the name of the copyright holder nor the names of its contributors
//     may be used to endorse or promote products derived from this software
//     without specific prior written permission.
//
//  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
//  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
//  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
//  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
//  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
//  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
//  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
//  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
//  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
//  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
//  POSSIBILITY OF SUCH DAMAGE.
//
//  SPDX-License-Identifier: BSD-3-Clause
//
//  @@-COPYRIGHT-END-@@
//
//==============================================================================


#include <algorithm>
#include <cstdint>
#include <cmath>
#include <stdexcept>
#include <cstdlib>
#include <climits>
#include <thread>
#include <vector>
#include <functional>

#include "DlQuantization/Quantization.hpp"
#include "trim_functions.hpp"

namespace DlQuantization
{
using namespace std;

inline double randUniformCpu()
{
    return rand() / (RAND_MAX + static_cast<double>(1.0));
}

double computeDelta(double encodingMin, double encodingMax, double numSteps, ScalingMode scaling_mode)
{
    // std::vector<double> _scale = {1., 0.5                        ,
    //    0.25                       , 0.125                      ,
    //    0.0625                     , 0.03125                    ,
    //    0.015625                   , 0.0078125                  ,
    //    0.00390625                 , 0.001953125                ,
    //    0.0009765625               , 0.00048828125              ,
    //    0.000244140625             , 0.0001220703125            ,
    //    0.00006103515625           , 0.000030517578125          ,
    //    0.0000152587890625         , 0.00000762939453125        ,
    //    0.000003814697265625       , 0.0000019073486328125      ,
    //    0.00000095367431640625     , 0.000000476837158203125    ,
    //    0.0000002384185791015625   , 0.00000011920928955078125  ,
    //    0.00000005960464477539063  , 0.000000029802322387695312 ,
    //    0.000000014901161193847656 , 0.000000007450580596923828 ,
    //    0.000000003725290298461914 , 0.000000001862645149230957 ,
    //    0.0000000009313225746154785, 0.0000000004656612873077393};
    
    double delta = (encodingMax - encodingMin) / numSteps;
    // printf("%d\n", scaling_mode);
    switch (scaling_mode)
    {



        case SCALE_POW2:
        {
            double _diff;
            double _min; 
            int _idx;
            int i;
            for(i=0;i<32;i++){
                _diff= abs(pow(2,-i) - delta);

                if (i == 0){
                    _min = _diff;
                    _idx = i;
                    }
                else {
                    if (_diff < _min){
                        _min = _diff;
                        _idx = i;
                    }
                }

            }
            // printf("==========%d===========\n", _idx);

            // return _scale[_idx];
            return pow(2, -_idx);


        }

        case SCALE_ORI:
        {


            return delta;

        }
    }








}


double computeOffset(double encodingMin, double delta)
{
    double offset = round(encodingMin / delta);

    return offset;
}


template <class Lambda>
Lambda parallelize(const uint32_t number_of_threads, Lambda lambda)
{
    std::vector<std::thread> threads(number_of_threads);
    for (uint32_t i = 0; i < number_of_threads; ++i)
    {
        threads[i] = std::thread(lambda, i);
    }
    for (uint32_t i = 0; i < number_of_threads; ++i)
    {
        threads[i].join();
    }
    return lambda;
};


// encoding: TF: rounded
template <typename DTYPE>
void quantizeDequantize(const DTYPE* in, int cnt, const TfEncoding& encoding, DTYPE* out,
                        ComputationMode mode_cpu_gpu, RoundingMode rounding_mode)
{
    // printf("=========%d==========\n", mode_cpu_gpu);
    switch (mode_cpu_gpu)
    {
    case COMP_MODE_CPU:
        quantizeDequantizeCpu(in, cnt, encoding, out, rounding_mode);
        break;
    case COMP_MODE_GPU:
#ifdef GPU_QUANTIZATION_ENABLED
            quantizeDequantizeGpu(in, cnt, encoding, out, rounding_mode);
#else
        throw runtime_error("Not compiled for GPU mode.");
#endif
        break;
    default:
        throw runtime_error("Unknown computation mode.");
        break;
    }
}

// encoding: TF: rounded
template <typename DTYPE>
void quantizeToFxp(const DTYPE* in, int cnt, const TfEncoding& encoding, DTYPE* out, ComputationMode mode_cpu_gpu,
                   RoundingMode rounding_mode, bool shiftToSigned)
{
    switch (mode_cpu_gpu)
    {
        case COMP_MODE_CPU:
            quantizeToFxpCpu(in, cnt, encoding, out, rounding_mode, shiftToSigned);
            break;
        case COMP_MODE_GPU:
#ifdef GPU_QUANTIZATION_ENABLED
            quantizeToFxpGpu(in, cnt, encoding, out, rounding_mode, shiftToSigned);
#else
            throw runtime_error("Not compiled for GPU mode.");
#endif
            break;
        default:
            throw runtime_error("Unknown computation mode.");
            break;
    }
}

// CPU implementations

template <typename DTYPE>
inline void quantizeValueCpu(const DTYPE* in, DTYPE* out,
                             DTYPE encoding_min, DTYPE encoding_max,
                             DTYPE encoding_delta, DTYPE encoding_offset,
                             RoundingMode rounding_mode)
{
    *out = fmax(fmin(*in, encoding_max), encoding_min);
    // Scale and add offset to get something in the range [0,2^bw-1]
    *out = *out / encoding_delta - encoding_offset;
    // printf("%d\n", rounding_mode);

    switch (rounding_mode)
    {



        case ROUND_NEAREST:
        {
            *out = round(*out);
            // *out = floor(*out);
            break;
        }
        case ROUND_STOCHASTIC:
        {
            *out = floor(*out + randUniformCpu());
            // *out = floor(*out);
            // printf("hi\n");

            break;
        }
        case ROUND_FLOOR:
        {
            *out = floor(*out);
            break;
        }
        default:
        {
            throw runtime_error("Unknown roundinggg mode.");
        }
    }
}

template <typename DTYPE>
inline void dequantizeValueCpu(DTYPE* out, DTYPE encoding_delta, DTYPE encoding_offset)
{
    *out = encoding_delta * (*out + encoding_offset);
}

template <typename DTYPE>
void quantizeDequantizeCpu(const DTYPE* in, int cnt, const TfEncoding& encoding, DTYPE* out,
                           RoundingMode rounding_mode)
{
    for (int i = 0; i < cnt; ++i)
    {
        quantizeValueCpu<DTYPE>(&in[i], &out[i],
                                encoding.min, encoding.max,
                                encoding.delta, encoding.offset,
                                rounding_mode);
        dequantizeValueCpu<DTYPE>(&out[i], encoding.delta, encoding.offset);
    }
}

template <typename DTYPE>
void quantizeToFxpPacked(const DTYPE* in, int cnt, const TfEncoding& encoding,
                         uint8_t* out, size_t out_size, ComputationMode mode_cpu_gpu,
                         RoundingMode rounding_mode, bool shiftToSigned)
{
    switch (mode_cpu_gpu) {
    case COMP_MODE_CPU:
      quantizeToFxpPackedCpu(in, cnt, encoding, out, out_size, rounding_mode, shiftToSigned);
      break;
    case COMP_MODE_GPU:
      throw runtime_error("GPU packed quantization not supported.");
      break;
    default:
      throw runtime_error("Unknown computation mode.");
      break;
  }

}
template <typename DTYPE>
void quantizeToFxpCpu(const DTYPE* in, int cnt, const TfEncoding& encoding, DTYPE* out, RoundingMode rounding_mode,
                      bool shiftToSigned)
{
    // Using unsigned int to account for case of signed symmetric 32 bit, when shift will be 2^31
    unsigned int shift = 0;
    if (shiftToSigned) {
        shift = pow(2, encoding.bw - 1);
    }
    for (int i = 0; i < cnt; ++i)
    {
        quantizeValueCpu<DTYPE>(&in[i], &out[i],
                                encoding.min, encoding.max,
                                encoding.delta, encoding.offset,
                                rounding_mode);
        out[i] -= shift;
    }
}

template <typename DTYPE>
void quantizeToFxpPackedCpu(const DTYPE* in, int cnt, const TfEncoding& encoding,
                           uint8_t* out, size_t out_size, RoundingMode rounding_mode, bool shiftToSigned)
{
    size_t min_out_size = ceil(max(encoding.bw, 8) * cnt / 8.0);
    if (out_size < min_out_size)
    {
        throw runtime_error("Out buffer is too small");
    }

    int number_of_threads = 4;   // determined by testing

#if 0
  if(encoding.bw < 8) {
    // Multi-threading not supported due to dependence between loop iterations
    number_of_threads = 1;
  }
#endif
    int iteration_per_threads = (int) ceil((double) cnt / (double) number_of_threads);
    auto quantize_job         = [&](int thread_id)
    {
        int start = thread_id * iteration_per_threads;
        int end   = std::min(start + iteration_per_threads, cnt);

        double data_quantized;
        for (int i = start; i < end; ++i)
        {
            // Saturate
            data_quantized = max(min((double) in[i], encoding.max), encoding.min);
            // Scale and add offset to get something in the range [0,2^bw-1]
            data_quantized = data_quantized / encoding.delta - encoding.offset;

            // Round
            // printf("%d\n", rounding_mode);
            switch (rounding_mode)
            {
            case ROUND_NEAREST:
            {
                data_quantized = round(data_quantized);
                break;
            }
            case ROUND_STOCHASTIC:
            {
                data_quantized = floor(data_quantized + randUniformCpu());
                break;
            }
            case ROUND_FLOOR:
            {
                data_quantized = floor(data_quantized);
                break;
            }
            default:
            {
                throw runtime_error("Unknown rounding mode.");
                break;
            }
            }

            // Pack the data according to the target bit-width and symmetry
            if (!shiftToSigned)
            {
                switch (encoding.bw)
                {
                case 1:
                case 2:
                case 4:
                {
                    // Note: this case should not be parallelized because the OR operation introduces dependency
                    // between iterations
                    uint8_t* ptr          = &out[0];
                    uint8_t data_shrinked = (uint8_t) data_quantized;
// Currently unsupported packed case
#if 0
                      int bit_offset = encoding.bw * i;
                      if (bit_offset % 8 == 0) {
                        // zero-out buffer on first write to byte
                        ptr[bit_offset / 8] = 0;
                      }
                      // OR-in data_shrinked
                      ptr[bit_offset / 8] |= (data_shrinked << (bit_offset % 8));
            // Supported one value per byte
#else
                    ptr[i] = (uint8_t) max(min((double) data_shrinked, double(pow(2, encoding.bw) - 1)), 0.0);

#endif
                    break;
                }
                case 8:
                {
                    uint8_t* ptr = &out[0];
                    ptr[i]       = (uint8_t) max(min(data_quantized, double(UCHAR_MAX)), 0.0);
                    break;
                }
                case 16:
                {
                    uint16_t* ptr = (uint16_t*) &out[0];
                    ptr[i]        = (uint16_t) max(min(data_quantized, double(USHRT_MAX)), 0.0);
                    break;
                }
                case 32:
                {
                    uint32_t* ptr = (uint32_t*) &out[0];
                    ptr[i]        = (uint32_t) max(min(data_quantized, double(UINT_MAX)), 0.0);
                    break;
                }
                default:
                {
                    throw runtime_error("Bit-width needs to be power of two and "
                                        "between 1 and 32.");
                }
                }   // end of switch encoding.bw
            }       // end of if (shiftToSigned)
            else
            {
                // Using unsigned int to account for case of signed symmetric i.e in the case of bw = 8, it will be -127 to 127
                double shift = 0;
                if (shiftToSigned) {
                    shift = pow(2, encoding.bw - 1) - 1;
                }
                data_quantized -=shift;
                // Pack the data according to the target bit-width ...
                switch (encoding.bw) {
                case 1:
                case 2:
                case 4:
                {
                    int8_t *ptr = (int8_t *) &out[0];
                    int8_t data_shrinked = (int8_t) data_quantized;
// Currently unsupported packed case
#if 0
              int bit_offset = encoding.bw * i;
              if (bit_offset % 8 == 0) ptr[bit_offset / 8] = 0; // zero-out buffer on first write to byte
              // OR-in data_shrinked
              ptr[bit_offset / 8] |= (data_shrinked * (int8_t)pow(2, (bit_offset % 8)));
#else
                    // Mask off the lower bw bits as a single byte
                    ptr[i] = data_shrinked & (int8_t)(pow(2,encoding.bw)-1);
#endif
                    break;
                }
                case 8: {
                    int8_t *ptr = (int8_t *) &out[0];
                    ptr[i] = (int8_t) max(min(data_quantized, double(SCHAR_MAX)), double(SCHAR_MIN));
                    break;
                }
                case 16: {
                    int16_t *ptr = (int16_t *) &out[0];
                    ptr[i] = (int16_t) max(min(data_quantized, double(SHRT_MAX)), double(SHRT_MIN));
                    break;
                }
                case 32: {
                    int32_t *ptr = (int32_t *) &out[0];
                    ptr[i] = (int32_t) max(min(data_quantized, double(INT_MAX)), double(INT_MIN));
                    break;
                }
                default: {
                    throw runtime_error("Bit-width needs to be power of two and "
                                        "between 1 and 32.");
                }
                } // End of switch(encoding.bw).
            }
        } // end of for loop
    };
    parallelize(number_of_threads, quantize_job);
}



template <typename DTYPE>
void dequantizeFromPackedFxp(const uint8_t* input, int cnt,
                             const TfEncoding& encoding, DTYPE* output,
                             ComputationMode mode_cpu_gpu, bool shiftToSigned) {
    switch (mode_cpu_gpu) {
    case COMP_MODE_CPU:
        dequantizeFromPackedFxpCpuMt(input, cnt, encoding, output, shiftToSigned);
        break;
    case COMP_MODE_GPU:
        throw runtime_error("GPU de-quantization not supported.");
        break;
    default:
        throw runtime_error("Unknown computation mode.");
        break;
    }
}

template <typename DTYPE>
void dequantizeFromPackedFxpCpuMt(const uint8_t* input, int cnt,
                                  const TfEncoding& encoding, DTYPE* output,
                                  bool shiftToSigned)
{
    int32_t num_threads = std::max(1, std::min(cnt/120000 , 4));
    int32_t chunkSize = cnt/num_threads;
    int32_t bw_adj    = encoding.bw/8;

    if (cnt % num_threads) {
        // add one to distribute remainder size evenly
        chunkSize++;
    }
    std::vector<std::thread> threads;
    for (int i = 0; i < num_threads; ++i) {
        int chunkStart = chunkSize*i;
        int chunkEnd   = std::min(chunkStart + chunkSize, cnt);
        threads.push_back(std::thread(dequantizeFromPackedFxpCpu<DTYPE>,
                                      input+(chunkStart*bw_adj),
                                      chunkEnd - chunkStart,
                                      encoding,
                                      output+chunkStart,
                                      shiftToSigned));
    }
    std::for_each(threads.begin(), threads.end(), std::mem_fn(&thread::join));
}


template <typename DTYPE>
void dequantizeFromPackedFxpTfBitsCpu(const uint8_t* input, int cnt,
                                       const TfEncoding& encoding, DTYPE* output) {
    double data_quantized;
    for (int i = 0; i < cnt; ++i) {
        // Extract next value from packed data stream
        // The packed data is unsigned in TF-style quantization
        int bit_offset = encoding.bw * i;
        uint32_t tmp = input[bit_offset / 8] >> (bit_offset % 8);
        data_quantized = (double)(tmp & (uint32_t)((1 << encoding.bw)-1));

        // De-quantize the data and write it to output vector.
        output[i] = ( encoding.delta * (data_quantized + encoding.offset));
    }
}

template <typename DTYPE>
void dequantizeFromPackedFxpTf8Cpu(const uint8_t* input, int cnt,
                                   const TfEncoding& encoding, DTYPE* output) {

    for (int i = 0; i < cnt; ++i) {
        // De-quantize the data and write it to output vector.
        output[i] = ( encoding.delta * ((double)input[i] + encoding.offset));
    }
}

template <typename DTYPE>
void dequantizeFromPackedFxpTf16Cpu(const uint16_t* input, int cnt,
                                     const TfEncoding& encoding, DTYPE* output) {

    for (int i = 0; i < cnt; ++i) {
        // De-quantize the data and write it to output vector.
        output[i] = ( encoding.delta * ((double)input[i] + encoding.offset));
    }
}

template <typename DTYPE>
void dequantizeFromPackedFxpTf32Cpu(const uint32_t* input, int cnt,
                                     const TfEncoding& encoding, DTYPE* output) {

    for (int i = 0; i < cnt; ++i) {
        // De-quantize the data and write it to output vector.
        output[i] = ( encoding.delta * ((double)input[i] + encoding.offset));
    }
}

template <typename DTYPE>
void dequantizeFromPackedFxpSymmetricBitsCpu(const uint8_t* input, int cnt,
                                              const TfEncoding& encoding, DTYPE* output) {
    double data_quantized;
    for (int i = 0; i < cnt; ++i) {
// Removed packed support, no current use case
#if 0
    // Extract next value from packed data stream
    // The packed data is signed for Qmn quantization
    int bit_offset = encoding.bw * i;
    int8_t* ptr = (int8_t*)input;
    // We need to extract a signed number from this byte. Take the byte,
    // shift it left until the MSB reaches the byte boundary, and shift it
    // down so the LSB reaches the byte boundary.
    int8_t tmp = ptr[bit_offset / 8] <<
                 (8 - bit_offset % 8 - encoding.bw);
    data_quantized = (double)(tmp >> (8 - encoding.bw));
#else
        int8_t* ptr = (int8_t*)input;
        // Mask the sign bit 2^(bw-1) and f negative apply to the upper MSB bits while retaining the
        // LSB for 2^(bw-1)-1. Eg 4bit # 0b00001011 is negative, and should become 0b11111011
        if (ptr[i] & (int8_t)pow(2,encoding.bw-1)) {
            data_quantized = ~((int8_t)pow(2,encoding.bw)-1) | ptr[i];
        } else {
            data_quantized = ptr[i];
        }

#endif

        // De-quantize the data and write it to output vector.
        output[i] = ( encoding.delta * (data_quantized + encoding.offset));
    }
}

template <typename DTYPE>
void dequantizeFromPackedFxpSymmetric8Cpu(const uint8_t* input, int cnt,
                                           const TfEncoding& encoding, DTYPE* output) {

    for (int i = 0; i < cnt; ++i) {
        int8_t* ptr = (int8_t*)input;
        // De-quantize the data and write it to output vector.
        output[i] = ( encoding.delta * ((double)ptr[i] + encoding.offset));
    }
}

template <typename DTYPE>
void dequantizeFromPackedFxpSymmetric16Cpu(const int16_t* input, int cnt,
                                            const TfEncoding& encoding, DTYPE* output) {

    for (int i = 0; i < cnt; ++i) {
        // De-quantize the data and write it to output vector.
        output[i] = ( encoding.delta * ((double)input[i] + encoding.offset));
    }
}

template <typename DTYPE>
void dequantizeFromPackedFxpSymmetric32Cpu(const int32_t* input, int cnt,
                                           const TfEncoding& encoding, DTYPE* output) {

    for (int i = 0; i < cnt; ++i) {
        // De-quantize the data and write it to output vector.
        output[i] = ( encoding.delta * ((double)input[i] + encoding.offset));
    }
}

template <typename DTYPE>
void dequantizeFromPackedFxpCpu(const uint8_t* input, int cnt,
                                const TfEncoding& encoding, DTYPE* output,
                                bool shiftToSigned)
{
    if (!shiftToSigned)
    {
        // Unpacking the data is bit-width specific
        switch (encoding.bw)
        {
            case 1:
            case 2:
            case 4:
                // Removing packed support since there's no current use case
                // Fall through to standard unsigned tf8 dequant since there's no difference

                // DeQuantizeFromPackedFxpTfBitsCpu(input, cnt, encoding, output);
            case 8:
                dequantizeFromPackedFxpTf8Cpu(input, cnt, encoding, output);
                break;
            case 16:
                dequantizeFromPackedFxpTf16Cpu((const uint16_t*) input, cnt, encoding, output);
                break;
            case 32:
                dequantizeFromPackedFxpTf32Cpu((const uint32_t*) input, cnt, encoding, output);
                break;
            default:
            {
                throw runtime_error("Bit-width needs to be power of two and "
                                    "between 1 and 32.");
            }
        }
    } else {
        // Unpacking the data is bit-width specific
        switch(encoding.bw) {
            case 1:
            case 2:
            case 4:
                dequantizeFromPackedFxpSymmetricBitsCpu(input, cnt, encoding, output);
                break;
            case 8:
                dequantizeFromPackedFxpSymmetric8Cpu(input, cnt, encoding, output);
                break;
            case 16:
                dequantizeFromPackedFxpSymmetric16Cpu((const int16_t*)input, cnt, encoding, output);
                break;
            case 32:
                dequantizeFromPackedFxpSymmetric32Cpu((const int32_t*)input, cnt, encoding, output);
                break;
            default: {
                throw runtime_error("Bit-width needs to be power of two and "
                                    "between 1 and 32.");
            }
        }
    }
}

// Explicit instantiations
template void quantizeDequantize(const double* in, int cnt, const TfEncoding& encoding, double* out,
                                 ComputationMode mode_cpu_gpu, RoundingMode rounding_mode);

template void quantizeDequantize(const float* in, int cnt, const TfEncoding& encoding, float* out,
                                 ComputationMode mode_cpu_gpu, RoundingMode rounding_mode);

template void quantizeToFxp(const double* in, int cnt, const TfEncoding& encoding, double* out,
                            ComputationMode mode_cpu_gpu, RoundingMode rounding_mode, bool shiftToSigned);

template void quantizeToFxp(const float* in, int cnt, const TfEncoding& encoding, float* out,
                            ComputationMode mode_cpu_gpu, RoundingMode rounding_mode, bool shiftToSigned);

template void quantizeToFxpPacked(const float* in, int cnt, const TfEncoding& encoding,
                                 uint8_t* out, size_t out_size, ComputationMode mode_cpu_gpu,
                                 RoundingMode rounding_mode, bool shiftToSigned);
template void quantizeToFxpPacked(const double* in, int cnt, const TfEncoding& encoding,
                                  uint8_t* out, size_t out_size, ComputationMode mode_cpu_gpu,
                                  RoundingMode rounding_mode, bool shiftToSigned);
template void dequantizeFromPackedFxp(const uint8_t* input, int cnt,
                                      const TfEncoding& encoding, double* output,
                                      ComputationMode mode_cpu_gpu, bool shiftToSigned);
template void dequantizeFromPackedFxp(const uint8_t* input, int cnt,
                                      const TfEncoding& encoding, float* output,
                                      ComputationMode mode_cpu_gpu, bool shiftToSigned);

}   // End of namespace DlQuantization
