#ifndef TS201_STDINT_H
#define TS201_STDINT_H
typedef int int32_t;
typedef unsigned int uint32_t;
typedef int intptr_t;
typedef unsigned int uintptr_t;
typedef int int_least32_t;
typedef unsigned int uint_least32_t;
typedef int int_fast32_t;
typedef unsigned int uint_fast32_t;
#define INT32_MIN (-2147483647 - 1)
#define INT32_MAX 2147483647
#define UINT32_MAX 4294967295U
#define INTPTR_MIN INT32_MIN
#define INTPTR_MAX INT32_MAX
#define UINTPTR_MAX UINT32_MAX
#define INT32_C(x) (x)
#define UINT32_C(x) ((unsigned int)(x))
#endif
