#include "fir.h"

int samples[] = {3, 5, 7, 9};
const int coefficients[] = {1, 2, 3, 4};
int result;

int main(void)
{
    result = dot(samples, coefficients, sizeof(samples) / sizeof(samples[0]));
    return result; /* 70 in XR0; also stored in result. */
}
