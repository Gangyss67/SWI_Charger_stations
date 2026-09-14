## Reservation domain
Charging stations on a parking lot

## Purpose
The system is used to reserve parking spaces with acces to charging stations with different charging speeds. The goal is to prevent situations where the parking lot is overcrowded or being used inefficiently.

## Users / Stakeholders
User – has the ability to create and cancel a reservation
Admin – has the ability to view all reservations and modify or cancel a reservation

## Core concepts
Reservation, Charging station, User, EV sorting (EV, plug-in hybrid)

## Core operations
- Create reservation
- Confirm / approve reservation
- Cancel reservation
- View reservation
- View parking space

## Persistent state
Specific parking slot, charging speed, time slot, user information (EV type, license plate)

## State-changing operation
DRAFT → CONFIRMED
CONFIRMED → CANCELLED
CONFIRMED → CANCELLED BY ADMIN
CONFIRMED → PENDING
PENDING → CANCELLED PREMATURELY

## Common business rule
Confirmed reservations for the same charging station must not overlap.

## Domain-specific business rule
Only an EV can par at a high-speed charging station, plug-in hybrids have to use slower chargers.

## External / system boundary
Notification Service – a parking space has opened up, a parking space with a higher charging speed has opened up

## Assumption
User discipline. Users arrive at the parking space and leave it on time – complying with reserved time slots

## Unknown
How the actual presence and identity (EV type and license plate) of the vehicle at the location will be verified.


## Selected future pressure
Category: Q

Concrete pressure:
A 10x increase in concurrent requests to reserve available slots and make reservations within a narrow time window.

Why it is relevant to our reservation system:
Confirmed reservations for the same station must not overlap. High levels of concurrent activity create a risk of race conditions (where two users attempt to book the same time slot at the same station within the same fraction of a second). The system must atomically verify both the availability of the slot and the fact that the fast chargers may only be occupied by EVs and not plug-in hybrids.