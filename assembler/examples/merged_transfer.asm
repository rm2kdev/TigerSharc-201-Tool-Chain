// PR 7-17..25: XY merge puts the high memory half in X; YX reverses it.
.entry start
.section text, code, 0x100
start:
    J0 = input;;
    K0 = output;;
    XYR0 = L[J0 + 0];;
    L[K0 + 0] = YXR0;;
    YXR3:2 = Q[J0 + 4];;
    Q[K0 + 4] = XYR3:2;;
    XYR7:4 = Q[J0 + 4];;
    XYR11:8 = XYR7:4;;
    J7:4 = XYR3:2;;
    XYR13:12 = J7:4;;
    K8 = J4;;
    J9 = K8;;
done: IDLE;;
.section inputs, data, 0x40000
input: .word 10, 20, 30, 40, 50, 60, 70, 80
.section outputs, data, 0x80000
output: .zero 8
