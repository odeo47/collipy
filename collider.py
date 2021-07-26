import logging
import logging.config
import os
import pickle
from server import GSH
from collections import Counter
import pandas as pd
import numpy as np
from timeit import default_timer as timer

logging.config.fileConfig('log/logging.conf')
logger = logging.getLogger('colliderLog')


class DecayMode:

    def __init__(self, vertex, track, ecal):
        """Decy mode object
        if argument value equals -1 it will be ignored. while comparing with other Decay mode it will accept every value

        Parameters
        ----------
        vertex: either int or DataFrame
            number of vertexes or vertex data frame
        track: either int or DataFrame
            number of tracks or track data frame
        ecal: either int or DataFrame
            number of ecal hits or ecal data frame
        """
        v = len(vertex) if type(vertex) == pd.DataFrame else vertex
        t = len(track) if type(track) == pd.DataFrame else track
        e = len(ecal) if type(ecal) == pd.DataFrame else ecal
        if type(v) == type(t) == type(e) == int:
            self.mode = v, t, e
        else:
            raise ValueError("Expected arguments to be either int or DataFrame type")

    def __le__(self, other):
        """Use <= as if 'self' mode contains 'other' mode"""
        if self.mode[0] == -1 or self.mode[0] == other.mode[0]:
            if self.mode[1] == -1 or self.mode[1] == other.mode[1]:
                if self.mode[2] == -1 or self.mode[2] == other.mode[2]:
                    return True
        return False

    def __ge__(self, other):
        return other.__le__(self)

    def __repr__(self):
        one = self.mode[0] if self.mode[0] != -1 else '?'
        two = self.mode[1] if self.mode[1] != -1 else '?'
        three = self.mode[2] if self.mode[2] != -1 else '?'
        return f'({one}, {two}, {three})'


class Injection:

    def __init__(self, vertex: pd.DataFrame, track: pd.DataFrame, ecal: pd.DataFrame):
        self.vertex = vertex
        self.track = track
        ecal.insert(1, 'dph', 0.05 * ecal['ph'])  # pulse height uncertainty was given by instructor: 5%
        self.ecal = ecal
        v, t, e = 0, 0, 0
        if 0 < len(vertex):
            v = np.max(
                np.abs([vertex.dx / vertex.x, vertex.dy / vertex.y, vertex.dz / vertex.z, vertex.dphi / vertex.phi]))
        if 0 < len(track):
            t = np.max(np.abs([track.dk / track.k, track.dtan_theta / track.tan_theta]))
        if 0 < len(ecal):
            e = np.max(np.abs([ecal.dx / ecal.x, ecal.dy / ecal.y, ecal.dz / ecal.z]))
        self.mode = DecayMode(len(vertex), len(track), len(ecal))
        self.max_rel = np.max([v, t, e])

    def __repr__(self):
        return f'({len(self.vertex)}, {len(self.track)}, {len(self.ecal)}) {self.max_rel:0.2g}'


class Data:

    def __init__(self, particle: str, momentum: float, data: list[Injection], mode: DecayMode, threshold: float,
                 cnt: Counter, m: int):
        """Container for injection's data that meet the following requirements:
           * particle's decay mode follows 'mode' decay
           * maximum relative uncertainty for each parameter calculated is smaller than 'threshold'
        Parameters
        ----------
        particle
            particle's name (as in README.md)
        momentum
            particle's momentum
        data
            list of injections
        mode
            decay mode that all the injections met
        threshold
            maximum relative uncertainty
        cnt
            counter for all modes encountered during the 'm' injections
        m
            number of injections required to create 'data' list
        """
        self.particle = particle
        self.momentum = momentum, 0.01 * momentum  # momentum uncertainty was given by instructor: 1%
        self.vertex, self.track, self.ecal = self._get_df(data)
        self.mode = mode
        self.threshold = threshold
        self.cnt = cnt
        self.m = m

    def __iter__(self):
        self._iter = []
        if 0 < len(self.vertex):
            self._iter.append(self.vertex.groupby(level=0).__iter__())
        else:
            self._iter.append(None)
        if 0 < len(self.track):
            self._iter.append(self.track.groupby(level=0).__iter__())
        else:
            self._iter.append(None)
        if 0 < len(self.ecal):
            self._iter.append(self.ecal.groupby(level=0).__iter__())
        else:
            self._iter.append(None)
        return self

    def __next__(self):
        v = self._iter[0].__next__()[1].droplevel(0) if self._iter[0] is not None else None
        t = self._iter[1].__next__()[1].droplevel(0) if self._iter[1] is not None else None
        e = self._iter[2].__next__()[1].droplevel(0) if self._iter[2] is not None else None
        return v, t, e

    @classmethod
    def load(cls, path: str):
        with open(path, 'rb') as f:
            # The protocol version used is detected automatically, so we do not have to specify it.
            data = pickle.load(f)
        return data

    @staticmethod
    def _get_df(data: list[Injection]):
        vframes, tframes, eframes = [], [], []
        for injection in data:
            vframes.append(injection.vertex)
            tframes.append(injection.track)
            eframes.append(injection.ecal)
        v = pd.concat(vframes, keys=pd.RangeIndex(len(vframes)))
        t = pd.concat(tframes, keys=pd.RangeIndex(len(tframes)))
        e = pd.concat(eframes, keys=pd.RangeIndex(len(eframes)))
        return v, t, e

    def dump(self, path: str):
        with open(path, 'wb') as f:
            # Pickle the 'data' dictionary using the highest protocol available.
            pickle.dump(self, f, pickle.HIGHEST_PROTOCOL)


class Collider:

    def __init__(self, user: str, password: str, alpha=1, filename='cal_func.pickle'):
        """ Collider object make performing injections seamless
        Parameters
        ----------
        user
            university username
        password
            university password
        alpha: float
            magnetic field multiplicity parameter: 'alpha' * B_0
            in the range [0.1, 10]
        filename: str
            file name of the calibration parameters fitted using calibration.py module
        """
        low = 0.1
        high = 10
        if not low <= alpha <= high:
            raise ValueError(f"alpha must meet the condition: {low} <= alpha <= {high}")
        self.alpha = alpha
        self._geant = GSH(user, password, alpha)
        self._momentum_beta = None
        self._energy_beta = None
        if filename in os.listdir('data'):
            with open(os.path.join('data', filename), 'rb') as f:
                cal = pickle.load(f)
            self._momentum_beta = cal['momentum']
            self._energy_beta = cal['energy']

    def _inject(self, particle: str, momentum: float, n: int) -> [Injection]:
        """Injecting $n particles and returns a list of Injection objects for successful injections
        therefore the length of the list is $n at most (may be less)"""
        lst = self._geant.inject(particle, momentum, n)
        res = []
        for tmp in lst:
            res.append(Injection(tmp[0], tmp[1], tmp[2]))
        return res

    def collect(self, particle: str, momentum: float, n: int, threshold: float, mode=DecayMode(-1, -1, -1)):
        """Collecting injections that meet the threshold and decay mode

        Parameters
        ----------
        particle
            particle name as stated at the README.md file
        momentum
            particle's momentum
        n
            collecting at least this number of injections that meet the conditions
        threshold
            max relative error for parameters in desired injection
        mode : DecayMode, default=None
            reflects the decay mode to collect, if 'None' ignore the mode
        prob : float
            estimated probability for the desired decay

        Returns
        -------
            data : Data
        """
        logger.info(f'Injecting {particle} {momentum} GeV')
        start = timer()
        data = []
        m = 0
        cnt = Counter()
        # Arbitrary lower bound for number of injections per loop
        times = n if 100 < n else 100
        while len(data) < n:
            lst = self._inject(particle, momentum, times)
            m += len(lst)
            for inj in lst:
                # progress bar - if you want to use it, add print() after the while loop
                print(f'\r{particle} {momentum:0.2g} GeV '
                      f'[{int(30 * len(data[:n]) / n) * "#" + (30 - int(30 * len(data[:n]) / n)) * "-"}] '
                      f'{int(100 * len(data[:n]) / n)}% completed '
                      f'{(timer() - start) / 60: 0.2g} mins', end='', flush=True)
                cnt[inj.mode.mode] += 1
                if inj.max_rel <= threshold:
                    if mode <= inj.mode:  # 'inj.mode' contains 'mode'
                        data.append(inj)
        print()
        data = Data(particle, momentum, data, mode, threshold, cnt, m)
        logger.info(f'Finished injecting {particle} {momentum} GeV')
        return data

    def kappa_pt(self, k: list, dk: list) -> (list, list):
        """calibration function for kappa-momentum
            ***note you can use this function only after calibration***
        Parameters
        ----------
        k
            list of kappas
        dk
            list of 'k' corresponding uncertainties

        Returns
        -------
            pt, dpt: list, list
        """
        if not self._momentum_beta:
            raise Exception('Calibration is needed first')
        beta = self._momentum_beta[0]
        dbeta = self._momentum_beta[1]
        pt = beta[0] / (k - beta[1])
        dpt = pt * np.sqrt((dbeta[0]/beta[0])**2 + (dk / (k - beta[1]))**2 + (dbeta[1] / (k - beta[1]))**2)
        return pt, dpt

    def ph_e(self, ph: list, dph: list) -> (list, list):
        """calibration function for pulse height-energy
            ***note you can use this function only after calibration***
        Parameters
        ----------
        ph
            list of pulse heights
        dph
            list of 'ph' corresponding uncertainties

        Returns
        -------
            e, de: list, list
        """
        if not self._energy_beta:
            raise Exception('Calibration is needed first')
        beta = self._energy_beta[0]
        dbeta = self._energy_beta[1]
        e = np.poly1d(beta)(ph)
        de = np.sqrt((dbeta[0]*ph)**2 + (beta[0]*dph)**2 + (dbeta[1])**2)
        return e, de
