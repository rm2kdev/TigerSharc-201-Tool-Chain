// Unexecuted DO loads must not access or validate the deliberately bad pointer.
.entry start
.section text, code, 0x100
start:
    J0 = 0x20000;;
    J1 = 0;;
    K0 = input;;
    XR0 = 99;;
    COMP(J1,0);;
    IF NJEQ; DO XR0 = [J0 + 0]; YR0 = [K0 + 0];;
    LC0 = 2;;
    IF LC0E; DO XR0 = [J0 + 0];;
done: IDLE;;
.section data, data, 0x40000
input: .word 42
