.entry start
.section text, code, 0x100
start:
    J0 = input;;
    K0 = output;;
    XYR1 = 6;;
    XYR2 = 7;;
    X R0 = R1 + R2; X R3 = R1 * R2 (I); Y R0 = R1 - R2; Y R3 = R1 * R2 (I);;
    XR4 = [J0 + 0];;
    YR4 = [J0 + 1];;
    X R8 = R1 + R2; XR5 = [J0 + 0]; YR5 = [K0 + 0];;
    [J0 + 2] = XR3; [K0 + 0] = YR3;;
    J1 = 0;;
    K1 = 0;;
    COMP(J1,0);;
    IF NJEQ; DO, J1 = J1 + 100; K1 = K1 + 7;;
    IF JEQ; J1 = J1 + 3; DO K1 = K1 + 5;;
    COMP(J1,0);;
    IF JEQ, JUMP wrong (NP); ELSE, J1 = J1 + 9; K1 = K1 + 11;;
    J31 = J1 - 12;;
    IF NJEQ, JUMP wrong (NP);;
done: IDLE;;
wrong: IDLE;;
.section inputs, data, 0x40000
input: .word 101, 202, 0, 0
.section outputs, data, 0x80000
output: .word 303, 0
