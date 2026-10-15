int factorial(int n)
{
    return n < 2 ? 1 : n * factorial(n - 1);
}

int main(void)
{
    return factorial(7); /* 5040 */
}
