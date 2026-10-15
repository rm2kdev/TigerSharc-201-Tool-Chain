.entry start
.section text, code, 0x100
start:
    XR1 = 7;;
    XR2 = 9;;
    YR1 = 13;;
    YR2 = 3;;
    J0 = 4;;
    K0 = 5;;
    X R3 = R1 + R2; Y R3 = R1 - R2; J1 = J0 + 2; K1 = K0 + 3;;
    J2 = J1 + 0x10000; X R4 = R1 * R2 (I); Y R4 = LSHIFT R2 BY 3;;
    COMP(J0,5);;
    IF JEQ, JUMP wrong (NP); ELSE X R5 = R1 + R3;;
    COMP(J2,0x10006);;
    IF JEQ, JUMP done (NP); ELSE Y R5 = R1 + R2;;
wrong: J10 = 99;;
done: IDLE;;
