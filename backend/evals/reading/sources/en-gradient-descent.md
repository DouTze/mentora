# Gradient Descent

Gradient descent is an iterative method for finding a local minimum of a differentiable function. At each step it moves the parameters a small distance in the direction opposite to the gradient, because the gradient points in the direction of steepest increase.

The size of each step is controlled by the learning rate. If the learning rate is too large, the updates can overshoot the minimum and the loss may diverge; if it is too small, convergence becomes very slow. Gradient descent is only guaranteed to approach a minimum when the learning rate is small enough for the function being optimized.

In practice, computing the gradient over the whole dataset is expensive, so stochastic gradient descent estimates it from a small random batch. The estimate is noisy, but on average it points in the right direction.
