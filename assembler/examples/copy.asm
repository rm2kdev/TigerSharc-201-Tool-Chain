// Copy four full-width values; post-update advances DSP WORD pointers.
.entry start
.section text, code, 0x120
start:
    J0 = source;;
    J1 = destination;;
    J2 = 4;;
copy_loop:
    J3 = [J0 += 1];;
    [J1 += 1] = J3;;
    J2 = J2 - 1;;
    COMP(J2, 0);;
    IF NJEQ, JUMP copy_loop (NP);;
    J4 = 0x12345678;;
    J5 = 0xFFFFFFFF;;
    J6 = J4 XOR J5;;
    J7 = J6 AND 127;;
    J8 = J7 OR 1;;
    J9 = J8;;
done: IDLE;;
.section inputs, data, 0x80000
source: .word 0, 0x7FFFFFFF, -2147483648, -1
.section results, data, 0xC0000
destination: .zero 4
source_pointer: .word source
