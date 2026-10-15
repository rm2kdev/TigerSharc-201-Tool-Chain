.entry start
.section text, code, 0x100
start:
    J0 = 5;;
    CALL add_seven (NP);;
    J1 = J0;;
done: IDLE;;
add_seven:
    J0 = J0 + 7;;
    CJMP (NP)(ABS);;
