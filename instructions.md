# Ticket Poller
The goal is a simple script that runs continuously to check the ticket master listing for Noah Kahan at protective stadium in April, and when 2 OR 3 resale tickets are uploaded (face value, not ridiculous scalped price), alert a given email

## Overview
- Poll [this website](https://www.ticketmaster.com/noah-kahan-the-last-of-the-birmingham-alabama-04-27-2027/event/2000655CAB442FB5) for "2 OR 3 resale tickets uploaded so I can go purchase them
- If the alert triggers, email details (link included) to
    - philip@ferreira-family.com
    - emery.c.little@gmail.com
- Once a day, email just me (philip) a canary check (if I don't see the email, I will assume the process went down)
- Expect it to run on this machine, and I will keep the machine running
- Written in Python (unless another language is deemed more suitable)
- Should handle network errors and retries gracefully

## Instructions
Do each in a subagent, you are coordinator and organizer, not the actual executor of the tasks.
- Gather requirements, what you need to know before starting the ticket polling script. For anything that is not immediately clear, ask me (the author) to clarify anything. Do not move on to implementation until all requirements are clear.
- Research existing solutions or libraries for ticket polling and email alerts in Python. Draw on precedence of open source/commonly used approaches and best practices.
- Make some test calls to the website to understand its structure and how ticket availability is presented, and if you're even able to programmatically access the necessary information.
- Design the architecture of the ticket polling script, including how to handle continuous polling, error handling, and email notifications.
- Review the design plan; bias heavily towards high-quality, readable, and maintainable code.
- Implement the ticket polling script based on the gathered requirements and research. If a piece of work is significant or complex, break it down into smaller, manageable tasks and implement them incrementally via separate subagents. For each subagents work, review it with another subagent, and surface a summary of changes to me. Do not move onto the next step until I approve.
- Review the overall script with several subagents as if they were reviewers, ensuring code quality, readability, and maintainability. Also make sure idiomatic Python (or chosen language) practices are followed.
- Test the script to ensure it correctly identifies ticket availability and sends email alerts as expected. Test alerts and such should only be sent to me (philip). Once it passes here, Emery's email will be added
- Deploy the script on the designated machine and set up any necessary scheduling or monitoring to ensure it runs continuously. There is  chance I will deploy this to a azure vm that is already running, so keep that in mind for the overall implementation.