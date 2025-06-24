* Copy the directory to your configuration folder and restart HA.
* Add `ASR proxy` integration.
* Enter the data for the primary and fallback STT servers.

  The first server can be [Speech-to-Phrase](https://github.com/OHF-Voice/speech-to-phrase), which returns an empty value on a miss, serving as a trigger to retry speech recognition on the backup server.
