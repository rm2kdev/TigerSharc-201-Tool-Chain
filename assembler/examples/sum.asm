// Original diagnostic: sum 1..10 in J, update K in parallel, store 55.
.entry start
.section text, code, 0x101
start:
    J0 = 0;;
    J1 = 1;;
    K0 = 100;;
    J10 = result;;
loop:
    J0 = J0 + J1; K0 = K0 - 1;;
    J1 = J1 + 1;;
    COMP(J1, 11);;
    IF NJEQ, JUMP loop (NP);;
    [J10 + 0] = J0;;
    J2 = [J10 + 0];;
done:
    IDLE;;
.section results, data, 0x40000
.align 4
result: .zero 1
