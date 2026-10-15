.global entry
.extern value
.section text, code, 4
entry:
    J0 = value;;
    XR0 = [J0 + 0];;
    IDLE;;
