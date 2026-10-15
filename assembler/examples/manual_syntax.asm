// Manual spellings executed end to end (PR Rev 1.1): unprefixed XY compute
// (2-7), auto one-slot loop branch with NOP slots (8-95), float immediates
// (10-86), CJMP = ADDRESS() (8-93), line NF from every-slot (NF) (A-14),
// an extended instruction outside slot 1 (1-23) and C expressions (A-16).
.entry start
.section text, code, 0x300
start:
    XR1 = 5;;
    XR2 = 7;;
    YR1 = 100;;
    YR2 = 1;;
    R3 = R1 + R2;;
    XR4 = 2.5;;
    J8 = (1 << 4) | 3;;
    XR8 = R1 + R2; J7 = 0x12345678;;
    J1 = 0;;
    LC0 = 3;;
loop:
    J1 = J1 + 1;;
    IF NLC0E, JUMP loop; NOP; NOP; NOP;;
    J5 = 1;;
    COMP(J5, 0);;
    IF NJEQ; DO J6 = J6 - J6 (NF);;
    IF JEQ, JUMP fail (NP);;
    CJMP = ADDRESS(target);;
    CJMP (ABS)(NP);;
fail:
    J9 = -1;;
    IDLE;;
target:
    J9 = 42;;
done: IDLE;;
