// COMP flag reference: the manual requires clearing JV and JC.
.entry start
.section text, code, 0x200
start:
    J0 = 1;;
    COMP(J0, 0);;
done: IDLE;;
