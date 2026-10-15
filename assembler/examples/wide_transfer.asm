.entry start
.section text, code, 0x100
start:
    J0 = input;;
    J1 = output;;
    XR3:0 = [J0 + 0];;
    YR3:0 = XR3:0;;
    [J1 + 0] = YR3:0;;
    XR5:4 = [J0 + 2];;
    X MR1:0 = R5:4;;
    X R7:6 = MR1:0;;
    J2 = J31 + 0x12345678;;
    K2 = K31 + -1000;;
    J3 = MIN(J2, -12345);;
    K3 = MAX(K2, 54321);;
done: IDLE;;
.section data, data, 0x40000
input: .word 0, 0xffffffff, 0x12345678, 0x80000000
output: .zero 4
