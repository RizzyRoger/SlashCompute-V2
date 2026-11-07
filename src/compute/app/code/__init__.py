"""Code tabs: a pool model reads and edits files in a folder you choose, under a permission mode.

- ``workspace``: safe paths and the file tools (list, read, search, previewed edits)
- ``commands``: how risky a terminal command is, and running it inside the folder
- ``policy``: the Plan / Auto / Manual permission matrix
- ``protocol``: the system prompt and the text tool-call format the model writes
- ``harness``: one tab's session and its agent loop; ``CodeManager`` keeps them all
- ``routes``: the ``/api/code/*`` endpoints of the app shell
"""
