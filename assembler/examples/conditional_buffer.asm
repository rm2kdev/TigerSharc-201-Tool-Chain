.entry start
.section text, code, 0x100
start:
    J0 = buffer;;
    JB0 = J31 + buffer;;
    JL0 = 4;;
    XR0 = CB [J0 += 1];;
    XR1 = CB [J0 += 1];;
    XR2 = CB [J0 += 1];;
    XR3 = CB [J0 += 1];;
    XR4 = CB [J0 += 1];;
    J2 = 0;;
    COMP(J2,0);;
    IF JEQ; DO J2 = J2 + 5;;
    IF JEQ; DO J2 = J2 + 100;;
    IF NJEQ (NF); DO K2 = K31 + 9;;
    IF NJEQ; DO XR5 = [J31 + buffer];;
done: IDLE;;
.section data, data, 0x40000
buffer: .word 11,22,33,44
