// Signed/unsigned comparisons, taken/untaken paths and a cross-bank branch.
.entry start
.section text, code, 0x140
start:
    J0 = -1;;
    J1 = 1;;
    COMP(J0, J1);;
    IF JLT, JUMP signed_ok (NP);;
    JUMP fail (NP);;
signed_ok:
    COMP(J0, J1) (U);;
    IF JLT, JUMP fail (NP);;
    IF NJLE, JUMP unsigned_ok (NP);;
    JUMP fail (NP);;
unsigned_ok:
    K0 = 0x80000000;;
    K1 = 0;;
    COMP(K0, K1);;
    IF KLE, JUMP k_ok (NP);;
    JUMP fail (NP);;
k_ok:
    COMP(K1, 0);;
    IF NKEQ, JUMP fail (NP);;
    IF KLT, JUMP fail (NP);;
    IF NKLT, JUMP far (NP)(ABS);;
fail:
    J9 = -1;;
    IDLE;;
.section far_text, code, 0x140003
far:
    J9 = 42;;
    K10 = result;;
    K2 = 42;;
    [K10 + 0] = K2;;
    K3 = [K10 + K1];;
done: IDLE;;
.section results, data, 0x100000
result: .zero 1
