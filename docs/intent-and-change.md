# Project Frame

## Reservation domain
Charging stations on a parking lot, reserved in 30-minute time slots.

## Purpose
The system is used to reserve charging stations with different charging speeds on a parking lot. The goal is to prevent situations where the parking lot is overcrowded or the chargers are being used inefficiently.

## Users / Stakeholders
- User – driver of an EV or a plug-in hybrid; can check availability, create, confirm and cancel their own reservations
- Admin – can view all reservations, approve / reject exception requests (vehicle too weak for a fast station) and cancel reservations

## Core concepts
- Reservation – a user's claim on one charging station for a time interval made of 30-minute slots
- Charging station (Resource) – one of 10 chargers with a given power in kW; 3 of them are high-speed (HPC); fast stations (≥ 50 kW: DC, HPC) admit automatically only vehicles that charge at least at half of the station power
- User – a person with an account and a vehicle type
- Vehicle model – from a catalog: EV (BEV) or plug-in hybrid (PHEV) and maximum DC charging power in kW; decides which stations the vehicle may use and whether a fast station needs an exception

## Core operations
- Check availability (OP-01)
- Create reservation (OP-02)
- Confirm reservation (OP-03)
- Cancel reservation (OP-04)
- Approve / reject reservation (OP-05, admin – since baseline v0.2)

## Persistent state
- Reservation: user, charging station, vehicle model, license plate, start time, end time (30-minute slots), state, created/updated timestamps, draft expiry, approval deadline
- Vehicle model: name, BEV / PHEV, maximum DC charging power (kW)
- Charging station: name, charging power (kW), whether it is high-speed
- User: username, password hash, default vehicle model, admin role

## State-changing operation
- DRAFT → CONFIRMED (user confirms: AC station, or vehicle strong enough for the fast station) or DRAFT → PENDING_APPROVAL (exception request: weaker vehicle on a fast station)
- PENDING_APPROVAL → CONFIRMED / REJECTED (admin approves / rejects)
- CONFIRMED → CANCELLED (user, at least 30 min before start) / CANCELLED_BY_ADMIN (admin, before end); PENDING_APPROVAL → CANCELLED
- DRAFT → EXPIRED after 5 min; PENDING_APPROVAL → EXPIRED when not decided by start − 15 min

## Common business rule
Reservations in a blocking state (CONFIRMED, PENDING_APPROVAL) for the same charging station must not overlap.

## Domain-specific business rule
Only an EV can park at a high-speed charging station, plug-in hybrids have to use slower chargers.

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
