split_amount divides a bill or a payout between people.
Amounts are integer cents, never floating-point values.
Each weight sets one party's portion relative to the others.
Equal weights describe an even split.
Shared-expense apps use a helper like this one.
A common case is one person paying for a group meal.
The result has one share per weight, in the same order.
The module uses only the Python standard library.
The function does not modify the weights list it receives.
A typical call splits a bill between two and six people.
Weights are often head counts or nights stayed.
The results are shown next to each person's name.
Other currencies use the same function with their minor units.
The function returns a new list on each call.
The web app and the reporting job share this helper.
Unit tests call it with small literal amounts.
