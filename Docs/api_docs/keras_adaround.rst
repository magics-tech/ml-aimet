:orphan:

.. _api-keras-adaround:

==================================
AIMET Keras AdaRound API
==================================

User Guide Link
===============
To learn more about this technique, please see :ref:`AdaRound<ug-adaround>`

Top-level API
=============
.. autofunction:: aimet_tensorflow.keras.adaround.adaround_weight.Adaround.apply_adaround

Adaround Parameters
===================
.. autoclass:: aimet_tensorflow.adaround.adaround_weight.AdaroundParameters
    :members:

Enum Definition
===============
**Quant Scheme Enum**

.. autoclass:: aimet_common.defs.QuantScheme
    :members:

Code Examples
=============

**Required imports**

.. literalinclude:: ../keras_code_examples/adaround.py
   :language: python
   :start-after: # AdaRound imports
   :end-before: # End of import statements

**Evaluation function**

.. literalinclude:: ../keras_code_examples/adaround.py
   :language: python
   :pyobject: dummy_forward_pass

**After applying AdaRound to the model, the AdaRounded model and associated encodings are returned**

.. literalinclude:: ../keras_code_examples/adaround.py
   :language: python
   :pyobject: apply_adaround_example
