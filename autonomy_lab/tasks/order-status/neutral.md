next_status models the life of an order in an online shop.
An order starts out pending.
It moves forward as payment and shipping happen.
Statuses and events are short lowercase strings.
Both lists are defined at the top of the module.
The web app and the warehouse service call this function.
Keeping the rules in one table makes them easy to review.
The function takes a status and an event and returns a status.
It has no side effects.
The order record is stored elsewhere and updated by the caller.
The module uses only the Python standard library.
Support staff see the resulting status on the order page.
Events are named with short verbs such as pay and ship.
A dictionary keyed by status and event is a common design.
The function does not send emails or notifications.
Tests call it directly with literal status and event names.
The same rules apply to every order, whatever its price.
Daily reports group orders by their current status.
