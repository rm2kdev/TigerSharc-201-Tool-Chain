#include "fir.h"

/* Small integer DSP kernel; deliberately no scaling or saturation extension. */
int dot(const int *samples, const int *coefficients, size_t count)
{
    int sum = 0;
    for (size_t i = 0; i < count; ++i)
        sum += samples[i] * coefficients[i];
    return sum;
}
