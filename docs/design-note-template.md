# <Title> design note

Use this for anything bigger than a small fix. Write it before any code. A reviewer checks it against VISION.md.

## Problem
What is wrong or missing, in plain words.

## Root cause
Where it first goes wrong, with the file and line.

## Approach
The change, and why it fixes the root cause rather than the symptom.

## Vision check
How this fits VISION.md. It must not add modes, single-use features, or answers that drop numbers or claims.

## What it could break
Callers, checks, data, and docs this touches. Say how each one is covered.

## Proving test
The test that fails today and passes after the change. Include both the case that should pass and the case that must still be caught.
