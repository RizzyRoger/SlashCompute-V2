# /compute terms — DRAFT

**DRAFT: these terms are a plain-language draft for a demo pool. They have not been reviewed by a lawyer.**

## What /compute is
/compute pools the memory of Macs and Windows PCs so that together they can run one large language model. You can use the pool to chat, and you can lend your computer to the pool to earn FLOPs.

## What runs on your computer when you contribute
- The app downloads a pinned build of **llama.cpp** and runs it on your computer.
- Your computer holds some **layers of a model** and does part of the work for other people's chat requests. It may also be chosen to hold the whole model file and lead a request (the "head").
- You choose how much memory to lend, as a **percentage** of your GPU memory (Windows), unified memory (Mac) or RAM (no GPU). The app keeps the model within that share. The rest of your computer stays yours, but it may run warmer and slower while it is serving.

## What data leaves your machine
- To take part, your computer talks to the main server: its name, chip, memory, the model files it holds, speed tests and network timings.
- **Other users' prompts and attachments pass through contributors' machines.** When you chat, your messages, images and text from attached files are processed by the computers in the pool, which belong to other people. When you contribute, other people's messages pass through your computer. Do not send anything you would not want a pool member to see.
- Attachments are read on your own computer first; nothing is sent until you press send.

## Stopping
You can stop contributing at any time. The app finishes the request it is serving, then stops. You can also quit the app.

## FLOPs
- You earn FLOPs one-for-one with the work your computer does. Chatting spends FLOPs for the work the pool does for you.
- **FLOPs have no cash value.** They cannot be exchanged for money and are only meaningful on this server.
- Purchases in the app are **TEST MODE**: no real money is taken and no payment service is contacted.

## Fair use
The server admin may flag or **ban accounts for abuse** (for example, cheating the FLOP counts, or misusing the pool). A banned account cannot chat or contribute.

## Changes
If these terms change, you will be asked to accept the new version before contributing again.
