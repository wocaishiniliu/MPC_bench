== Example 2: SPDZ-56 - MPC Compiler Type System ==

This task fixes type handling in MP-SPDZ's compiler (Compiler/types.py) that determines how high-level MPC operations are compiled into protocol instructions. Below are key changes:

1. Thread execution model (how protocols run):

   class MPCThread(object):
       - def __init__(self, target, name, args=[], runtime_arg=None):
       + def __init__(self, target, name, args=[], runtime_arg=0,
       +              single_thread=False):
           ...
           - self.running = 0
           + self.running = 0
           + self.tape_handle = program.new_tape(target, args, name,
           +                                      single_thread=single_thread)
           + self.run_handles = []

       def start(self, runtime_arg=None):
           self.running += 1
           - program.start_thread(self, runtime_arg or self.runtime_arg)
           + self.run_handles.append(program.run_tape(self.tape_handle,
           +                          runtime_arg or self.runtime_arg))

   This changes how the compiler manages protocol execution threads. The old model used direct thread management; the new model uses tape handles for protocol compilation. This affects how multi-party protocols coordinate execution.

2. Vector operation error handling:

   def vectorize_max(operation):
       ...
       set_global_vector_size(size)
       - res = operation(self, *args, **kwargs)
       - reset_global_vector_size()
       + try:
       +     res = operation(self, *args, **kwargs)
       + finally:
       +     reset_global_vector_size()
       return res

   This ensures vector size state is properly reset even when operations fail. In MPC, vector operations on secret types must maintain consistent size semantics across parties. Incorrect state cleanup can cause protocol desynchronization.

Why this is compiler-level protocol work:

The type system in MP-SPDZ determines which MPC protocol is used for each operation. When you write sint (secret integer) or sfix (secret fixed-point), the compiler's type system:
- Selects the appropriate protocol (Shamir, replicated, or garbled circuit)
- Determines communication patterns between parties
- Manages how operations compile to protocol instructions
- Ensures type conversions use correct protocols (e.g., A2B conversion)

These fixes address how the compiler orchestrates protocol execution (tape management) and maintains type state (vector size). The full patch includes extensive refactoring, but these core changes show the compiler manages MPC protocol semantics, not just Python syntax.

This directly addresses: protocol selection (compiler determines which protocols execute for each type/operation).

== Example 3: CrypTen-223 - Binary Comparison Circuits ==

This task fixes binary comparison protocols broken by PyTorch's change from logical to arithmetic right-shift. Below are the core fixes:

1. Fix the parallel prefix circuit:

   def __P_circuit(P):
       shift = __BITS // 2
       for _ in range(__LOG_BITS):
           - P &= P >> shift  # BUG: arithmetic shift sign-extends
           + P &= P << shift  # FIX: use left shift due to new semantics
           shift //= 2
       return P

2. Add helper to extract sign bit correctly under arithmetic shift:

   def __get_sign_bit(x):
       y = x >> 63
       # Arithmetic shift fills with sign bit, so y is now all -1 or all 0
       # Need to map -1 -> 1, 0 -> 0 for protocol correctness
       if isinstance(y, BinarySharedTensor):
           y.share = y.share.eq(-1).long()
       else:
           y = y.eq(-1).long()
       return y

3. Update comparison operations to use the corrected sign extraction:

   def lt(x, y):
       S, P = ...(compute using __P_circuit)...
       - return S >> (__BITS - 1)  # BUG: arithmetic shift breaks this
       + return __get_sign_bit(S)  # FIX: correct sign extraction

The implementation requires understanding:
- Parallel prefix circuits (Kogge-Stone adder) for oblivious comparison
- How arithmetic shift (sign-extends) vs logical shift (zero-fills) affects secret-shared binary values
- That you cannot simply swap >> and << operators—the circuit logic must be redesigned
- Bit-level protocol correctness where a single wrong bit breaks the entire comparison

The complete patch modifies crypten/mpc/primitives/circuit.py with 79 lines of changes, with the core logic shown above.

This directly addresses: data-oblivious algorithm design at the circuit level.

