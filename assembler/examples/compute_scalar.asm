// Original fixed/float/shifter/multiplier and X/Y parallel execution exercise.
.entry start
.section text, code, 0x100
start:
    XR1 = 7;;
    XR2 = -3;;
    YR1 = 11;;
    YR2 = 5;;
    X R3 = R1 + R2; Y R3 = R1 - R2;;
    X R4 = R1 * R2 (I);;
    X R5 = LSHIFT R1 BY 3;;
    X R6 = ASHIFT R2 BY -1;;
    X R7 = ABS R2;;
    X R8 = MIN(R1, R2);;
    X R9 = MAX(R1, R2);;
    X R10 = BSET R1 BY 31;;
    X R11 = LD0 R1;;
    X R12 = NOT R1;;
    XR16 = 0x3fc00000;;       // 1.5f
    XR17 = 0x40100000;;       // 2.25f
    X FR18 = R16 + R17;;
    X FR19 = R16 * R17;;
    X FR20 = FLOAT R1;;
    X R21 = FIX FR17 (T);;
    X FCOMP(R16, R17);;
    IF XALT, JUMP float_less (NP);;
    J0 = -1;;
    JUMP done (NP);;
float_less:
    J0 = 1;;
done: IDLE;;
