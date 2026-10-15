// Console output is a host convention at DSP word address 0xFFFF0000.
// It is not a physical TS201 peripheral. Each store emits its low byte.
.entry start
.section text, code, 0x100
start:
    J0 = message;;
    K0 = 0xFFFF0000;;
next_character:
    J1 = [J0 += 1];;
    COMP(J1, 0);;
    IF JEQ, JUMP done (NP);;
    [K0 + 0] = J1;;
    JUMP next_character (NP);;
done:
    IDLE;;
.section data, data, 0x40000
message:
    .word 72, 101, 108, 108, 111, 44, 32, 119, 111, 114, 108, 100, 33, 10, 0
